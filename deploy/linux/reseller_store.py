"""Additive reseller accounts and ownership; never modifies WG_Panel's database."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from werkzeug.security import generate_password_hash, check_password_hash


class StoreError(ValueError):
    pass


class ResellerStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self, write=False):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            connection.close()
            raise StoreError('نسخهٔ داده‌های نمایندگی جدیدتر از این برنامه است.')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS resellers (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                expires_at INTEGER NOT NULL DEFAULT 0, max_peers INTEGER NOT NULL DEFAULT 100,
                interfaces TEXT NOT NULL DEFAULT '[]', auth_version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS reseller_peers (
                peer_id INTEGER PRIMARY KEY, reseller_id INTEGER NOT NULL REFERENCES resellers(id),
                public_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS reseller_operations (
                id INTEGER PRIMARY KEY, reseller_id INTEGER NOT NULL REFERENCES resellers(id),
                request_id TEXT NOT NULL, state TEXT NOT NULL, peer_id INTEGER,
                created_at INTEGER NOT NULL, UNIQUE(reseller_id,request_id));
            CREATE TABLE IF NOT EXISTS reseller_audit (
                id INTEGER PRIMARY KEY, reseller_id INTEGER, action TEXT NOT NULL,
                actor TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS reseller_login_failures (
                identity TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS reseller_failures_idx ON reseller_login_failures(identity,created_at);
            CREATE TABLE IF NOT EXISTS reseller_renewals (
                reseller_id INTEGER NOT NULL REFERENCES resellers(id), request_id TEXT NOT NULL,
                days INTEGER NOT NULL, actor TEXT NOT NULL, expires_at INTEGER NOT NULL,
                UNIQUE(reseller_id, request_id));
            PRAGMA user_version=2;
        ''')
        if os.name != 'nt':
            os.chmod(self.path, 0o600)
        try:
            if write:
                connection.execute('BEGIN IMMEDIATE')
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def public(row):
        result = dict(row)
        result.pop('password_hash', None)
        result['interfaces'] = json.loads(result['interfaces'])
        result['enabled'] = bool(result['enabled'])
        result['active'] = result['enabled'] and result['expires_at'] > int(time.time())
        return result

    def get(self, reseller_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            return self.public(row) if row else None

    def all(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM resellers ORDER BY id DESC').fetchall()
            return [self.public(row) for row in rows]

    def create(self, username, password, interfaces, max_peers=100, actor='admin'):
        username = username.strip()
        if not 1 <= len(username) <= 64 or any(ord(c) < 32 for c in username):
            raise StoreError('نام کاربری نامعتبر است.')
        if not 12 <= len(password) <= 1024:
            raise StoreError('رمز باید بین ۱۲ تا ۱۰۲۴ نویسه باشد.')
        interfaces = sorted(set(int(i) for i in interfaces))
        if not interfaces or any(i < 1 for i in interfaces) or not 1 <= max_peers <= 10000:
            raise StoreError('Interface و سقف مشتری را بررسی کنید.')
        hashed = generate_password_hash(password)
        try:
            with self.db(write=True) as db:
                cursor = db.execute('''INSERT INTO resellers
                    (username,password_hash,interfaces,max_peers,created_at) VALUES(?,?,?,?,?)''',
                    (username, hashed, json.dumps(interfaces), max_peers, int(time.time())))
                reseller_id = cursor.lastrowid
                db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                           (reseller_id, 'created', actor, int(time.time())))
        except sqlite3.IntegrityError as exc:
            raise StoreError('این نام کاربری قبلاً ثبت شده است.') from exc
        return self.get(reseller_id)

    def authenticate(self, username, password, identity):
        now = int(time.time())
        ip_bucket = 'ip:' + identity
        account_bucket = 'account:' + username.strip().casefold()
        with self.db(write=True) as db:
            db.execute('DELETE FROM reseller_login_failures WHERE created_at<?', (now - 900,))
            if (db.execute('SELECT count(*) FROM reseller_login_failures WHERE identity=?', (ip_bucket,)).fetchone()[0] >= 10 or
                    db.execute('SELECT count(*) FROM reseller_login_failures WHERE identity=?', (account_bucket,)).fetchone()[0] >= 10):
                return None, True
            row = db.execute('SELECT * FROM resellers WHERE username=? COLLATE NOCASE', (username.strip(),)).fetchone()
            # Equal-cost password verification when the username does not exist.
            hashed = row['password_hash'] if row else _DUMMY_HASH
            accepted = check_password_hash(hashed, password)
            if not row or not accepted:
                db.executemany('INSERT INTO reseller_login_failures VALUES(?,?)', ((ip_bucket, now), (account_bucket, now)))
                return None, False
            # A successful account must not erase another account's IP failures.
            return self.public(row), False

    def update(self, reseller_id, *, days=None, enabled=None, actor='admin', request_id=None):
        if days is not None and (type(days) is not int or not 1 <= days <= 366):
            raise StoreError('مدت تمدید باید بین ۱ تا ۳۶۶ روز باشد.')
        if enabled is not None and type(enabled) is not bool:
            raise StoreError('وضعیت حساب نامعتبر است.')
        if days is None and enabled is None:
            raise StoreError('تغییری مشخص نشده است.')
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            if not row:
                raise StoreError('نماینده پیدا نشد.')
            if days is not None:
                if request_id:
                    previous = db.execute('SELECT * FROM reseller_renewals WHERE reseller_id=? AND request_id=?', (reseller_id, request_id)).fetchone()
                    if previous:
                        if previous['days'] != days or previous['actor'] != actor:
                            raise StoreError('شناسهٔ درخواست برای تمدید دیگری استفاده شده است.')
                        return self.public(row)
                expires = max(int(time.time()), row['expires_at']) + days * 86400
                db.execute('UPDATE resellers SET expires_at=? WHERE id=?', (expires, reseller_id))
                if request_id:
                    db.execute('INSERT INTO reseller_renewals VALUES(?,?,?,?,?)', (reseller_id, request_id, days, actor, expires))
                action = 'renew:' + str(days)
            else:
                db.execute('UPDATE resellers SET enabled=? WHERE id=?', (int(enabled), reseller_id))
                action = 'enabled' if enabled else 'paused'
            db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                       (reseller_id, action, actor, int(time.time())))
        return self.get(reseller_id)

    def ownership(self, reseller_id):
        with self.db() as db:
            return {row['peer_id']: row['public_key'] for row in db.execute(
                'SELECT peer_id,public_key FROM reseller_peers WHERE reseller_id=?', (reseller_id,))}

    def reserve(self, reseller_id, request_id):
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            if not row or not self.public(row)['active']:
                raise StoreError('اعتبار پنل پایان یافته یا حساب متوقف شده است.')
            operation = db.execute('SELECT * FROM reseller_operations WHERE reseller_id=? AND request_id=?',
                                   (reseller_id, request_id)).fetchone()
            if operation:
                return dict(operation)
            count = db.execute('SELECT count(*) FROM reseller_peers WHERE reseller_id=?', (reseller_id,)).fetchone()[0]
            pending = db.execute("SELECT count(*) FROM reseller_operations WHERE reseller_id=? AND state IN ('pending','uncertain')", (reseller_id,)).fetchone()[0]
            if count + pending >= row['max_peers']:
                raise StoreError('ظرفیت ساخت مشتری جدید تکمیل شده است.')
            cursor = db.execute('INSERT INTO reseller_operations(reseller_id,request_id,state,created_at) VALUES(?,?,?,?)',
                                (reseller_id, request_id, 'pending', int(time.time())))
            return {'id': cursor.lastrowid, 'state': 'new'}

    def finish(self, operation_id, reseller_id, peer_id, public_key):
        with self.db(write=True) as db:
            db.execute('INSERT INTO reseller_peers VALUES(?,?,?,?)', (peer_id, reseller_id, public_key, int(time.time())))
            db.execute("UPDATE reseller_operations SET state='complete',peer_id=? WHERE id=? AND reseller_id=?",
                       (peer_id, operation_id, reseller_id))

    def failed(self, operation_id, *, uncertain=False):
        with self.db(write=True) as db:
            db.execute('UPDATE reseller_operations SET state=? WHERE id=?', ('uncertain' if uncertain else 'failed', operation_id))

    def audit(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM reseller_audit ORDER BY id DESC LIMIT 100')]

    def operations(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM reseller_operations WHERE state IN ('pending','uncertain') ORDER BY id DESC LIMIT 100")]


_DUMMY_HASH = generate_password_hash('not-a-real-user-password')
