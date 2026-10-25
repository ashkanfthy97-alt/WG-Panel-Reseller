"""Durable first-run state for the browser setup service.

All transitions use a short SQLite write transaction. Network, ACME, and
WG_Panel calls deliberately happen outside those transactions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
from contextlib import contextmanager


class SetupStateError(Exception):
    pass


class InvalidSetupToken(SetupStateError):
    pass


class SetupAlreadyClaimed(SetupStateError):
    pass


class SetupAlreadyComplete(SetupStateError):
    pass


class OwnerClaimInProgress(SetupStateError):
    pass


class InvalidTransition(SetupStateError):
    pass


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SetupState:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            pass
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self, token: str, public_ip: str) -> None:
        if len(token) < 32:
            raise ValueError("The first-run token must contain at least 32 characters")
        with self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS setup_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    phase TEXT NOT NULL,
                    public_ip TEXT NOT NULL,
                    token_hash TEXT,
                    owner_claimed INTEGER NOT NULL DEFAULT 0,
                    claim_id TEXT,
                    owner_username TEXT,
                    domain TEXT,
                    tls_ready INTEGER NOT NULL DEFAULT 0,
                    cert_name TEXT,
                    cert_path TEXT,
                    panel_ready INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            row = db.execute("SELECT public_ip FROM setup_state WHERE id=1").fetchone()
            if row is None:
                db.execute("""
                    INSERT INTO setup_state (id, phase, public_ip, token_hash)
                    VALUES (1, 'awaiting_owner', ?, ?)
                """, (public_ip, _token_digest(token)))
            elif row["public_ip"] != public_ip:
                raise SetupStateError(
                    "Stored setup IP differs from the detected IP; use the recovery command to rotate it."
                )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def read(self) -> dict:
        if not self.path.is_file():
            raise SetupStateError("Setup has not been initialized")
        try:
            with self._db() as db:
                row = db.execute("SELECT * FROM setup_state WHERE id=1").fetchone()
        except sqlite3.Error as exc:
            raise SetupStateError("Setup storage is not initialized or unavailable") from exc
        if row is None:
            raise SetupStateError("Setup has not been initialized")
        result = dict(row)
        result.pop("token_hash", None)
        result["owner_claimed"] = bool(result["owner_claimed"])
        result["tls_ready"] = bool(result["tls_ready"])
        result["panel_ready"] = bool(result["panel_ready"])
        result["token_available"] = self._has_token()
        return result

    def _has_token(self) -> bool:
        with self._db() as db:
            row = db.execute("SELECT token_hash FROM setup_state WHERE id=1").fetchone()
        return bool(row and row["token_hash"])

    def begin_owner_claim(self, token: str, username: str) -> dict:
        if not username or len(username.strip()) > 64:
            raise ValueError("Administrator username must contain 1–64 characters")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM setup_state WHERE id=1").fetchone()
            if row is None:
                raise SetupStateError("Setup has not been initialized")
            if row["phase"] == "complete":
                raise SetupAlreadyComplete()
            if row["owner_claimed"]:
                raise SetupAlreadyClaimed()
            if row["phase"] == "claiming":
                raise OwnerClaimInProgress()
            token_hash = row["token_hash"] or ""
            if not token_hash or not hmac.compare_digest(token_hash, _token_digest(token)):
                raise InvalidSetupToken()
            claim_id = secrets.token_urlsafe(24)
            db.execute("""
                UPDATE setup_state
                SET phase='claiming', claim_id=?, owner_username=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            """, (claim_id, username.strip()))
            db.commit()
            return {"reserved": True, "claim_id": claim_id}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def complete_owner_claim(self, claim_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("""
                UPDATE setup_state
                SET phase='owner_claimed', owner_claimed=1, claim_id=NULL,
                    token_hash=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=1 AND phase='claiming' AND claim_id=?
            """, (claim_id,))
            if result.rowcount != 1:
                db.rollback()
                raise InvalidTransition("Owner claim reservation is no longer valid")
            db.commit()

    def recover_owner_claim(self, claim_id: str, admin_exists: bool) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM setup_state WHERE id=1").fetchone()
            if row is None or row["phase"] != "claiming" or row["claim_id"] != claim_id:
                db.rollback()
                raise InvalidTransition("Owner claim reservation is no longer valid")
            if admin_exists:
                db.execute("""
                    UPDATE setup_state
                    SET phase='owner_claimed', owner_claimed=1, claim_id=NULL,
                        token_hash=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id=1
                """)
            else:
                db.execute("""
                    UPDATE setup_state
                    SET phase='awaiting_owner', claim_id=NULL, owner_username=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=1
                """)
            db.commit()

    def recover_stale_owner_claim(self, stale_seconds: int = 180) -> bool:
        """Release a crashed claim only after its short reservation lease expires."""
        if not 30 <= int(stale_seconds) <= 3600:
            raise ValueError("Stale claim timeout must be between 30 and 3600 seconds")
        modifier = f"-{int(stale_seconds)} seconds"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("""
                UPDATE setup_state
                SET phase='awaiting_owner', claim_id=NULL, owner_username=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1 AND phase='claiming'
                  AND datetime(updated_at) <= datetime('now', ?)
            """, (modifier,))
            db.commit()
            return result.rowcount == 1

    def mark_owner_exists(self, username: str = "") -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE setup_state
                SET phase=CASE WHEN phase='complete' THEN phase ELSE 'owner_claimed' END,
                    owner_claimed=1, claim_id=NULL, token_hash=NULL,
                    owner_username=COALESCE(NULLIF(owner_username,''), NULLIF(?,'')),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            """, (username.strip(),))
            db.commit()

    def set_domain(self, domain: str, status: str) -> None:
        if status not in {"pending", "issued", "failed"}:
            raise ValueError("Invalid domain status")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT phase, owner_claimed FROM setup_state WHERE id=1").fetchone()
            if row is None or not row["owner_claimed"] or row["phase"] == "complete":
                db.rollback()
                raise InvalidTransition("Domain setup requires an unfinished owner-claimed setup")
            phase = "domain_pending" if status == "pending" else "owner_claimed"
            db.execute("""
                UPDATE setup_state
                SET domain=?, phase=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            """, (domain, phase))
            db.commit()

    def mark_tls_ready(self, cert_name: str, cert_path: str, domain: str | None = None,
                       *, allow_complete: bool = False) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_claimed, phase FROM setup_state WHERE id=1").fetchone()
            if row is None or (row["phase"] == "complete" and not allow_complete):
                db.rollback()
                raise InvalidTransition("TLS cannot be changed after setup is complete")
            db.execute("""
                UPDATE setup_state
                SET tls_ready=1, cert_name=?, cert_path=?, domain=COALESCE(?,domain),
                    phase=CASE WHEN phase='complete' THEN phase WHEN owner_claimed=1 THEN 'owner_claimed' ELSE phase END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            """, (cert_name, cert_path, domain))
            db.commit()

    def restore_tls_state(self, snapshot: dict) -> None:
        """Restore only certificate fields; never reopen a consumed owner claim."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE setup_state SET domain=?, tls_ready=?, cert_name=?, cert_path=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=1
            """, (snapshot.get("domain"), int(bool(snapshot.get("tls_ready"))),
                  snapshot.get("cert_name"), snapshot.get("cert_path")))
            db.commit()

    def complete_setup(self, panel_ready: bool) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_claimed, tls_ready FROM setup_state WHERE id=1").fetchone()
            if row is None or not row["owner_claimed"] or not row["tls_ready"] or not panel_ready:
                db.rollback()
                raise InvalidTransition("Setup requires an owner, encrypted TLS, and a healthy panel")
            db.execute("""
                UPDATE setup_state
                SET phase='complete', panel_ready=1, token_hash=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            """)
            db.commit()

    def rotate_token(self, token: str) -> None:
        if len(token) < 32:
            raise ValueError("The first-run token must contain at least 32 characters")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT phase, owner_claimed FROM setup_state WHERE id=1").fetchone()
            if row is None or row["owner_claimed"] or row["phase"] == "complete":
                db.rollback()
                raise InvalidTransition("Setup token can only be rotated before owner creation")
            db.execute("""
                UPDATE setup_state
                SET token_hash=?, phase='awaiting_owner', claim_id=NULL,
                    owner_username=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            """, (_token_digest(token),))
            db.commit()
