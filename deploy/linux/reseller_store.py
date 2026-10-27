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
        try:
            self._initialize(connection)
            if os.name != 'nt':
                os.chmod(self.path, 0o600)
            if write:
                connection.execute('BEGIN IMMEDIATE')
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _initialize(connection):
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version > 4:
            raise StoreError('نسخهٔ داده‌های نمایندگی جدیدتر از این برنامه است.')
        if version == 4:
            return
        # Serialise additive upgrades across workers and re-read the version
        # after obtaining the lock. executescript would commit this lock early.
        connection.execute('BEGIN IMMEDIATE')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version > 4:
            raise StoreError('نسخهٔ داده‌های نمایندگی جدیدتر از این برنامه است.')
        if version == 4:
            connection.commit()
            return
        statements = (
            '''CREATE TABLE IF NOT EXISTS resellers (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                expires_at INTEGER NOT NULL DEFAULT 0, max_peers INTEGER NOT NULL DEFAULT 100,
                interfaces TEXT NOT NULL DEFAULT '[]', auth_version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                parent_id INTEGER REFERENCES resellers(id),
                creation_fee_toman INTEGER NOT NULL DEFAULT 0 CHECK(creation_fee_toman >= 0))''',
            '''CREATE TABLE IF NOT EXISTS reseller_peers (
                peer_id INTEGER PRIMARY KEY, reseller_id INTEGER NOT NULL REFERENCES resellers(id),
                public_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS reseller_operations (
                id INTEGER PRIMARY KEY, reseller_id INTEGER NOT NULL REFERENCES resellers(id),
                request_id TEXT NOT NULL, state TEXT NOT NULL, peer_id INTEGER,
                created_at INTEGER NOT NULL, UNIQUE(reseller_id,request_id))''',
            '''CREATE TABLE IF NOT EXISTS reseller_audit (
                id INTEGER PRIMARY KEY, reseller_id INTEGER, action TEXT NOT NULL,
                actor TEXT NOT NULL, created_at INTEGER NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS reseller_login_failures (
                identity TEXT NOT NULL, created_at INTEGER NOT NULL)''',
            '''CREATE INDEX IF NOT EXISTS reseller_failures_idx ON reseller_login_failures(identity,created_at)''',
            '''CREATE TABLE IF NOT EXISTS reseller_renewals (
                reseller_id INTEGER NOT NULL REFERENCES resellers(id), request_id TEXT NOT NULL,
                days INTEGER NOT NULL, actor TEXT NOT NULL, expires_at INTEGER NOT NULL,
                UNIQUE(reseller_id, request_id))''',
        )
        for statement in statements:
            connection.execute(statement)
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(resellers)')}
        if 'parent_id' not in columns:
            connection.execute('ALTER TABLE resellers ADD COLUMN parent_id INTEGER REFERENCES resellers(id)')
        if 'creation_fee_toman' not in columns:
            connection.execute('''ALTER TABLE resellers ADD COLUMN creation_fee_toman
                INTEGER NOT NULL DEFAULT 0 CHECK(creation_fee_toman >= 0)''')
        # Existing accounts retain their established access. Only create() below
        # inserts pending accounts; the migration never rewrites their credentials.
        if 'approval_status' not in columns:
            connection.execute("ALTER TABLE resellers ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'approved'")
        if 'approved_at' not in columns:
            connection.execute('ALTER TABLE resellers ADD COLUMN approved_at INTEGER')
            connection.execute('UPDATE resellers SET approved_at=created_at')
        if 'approved_by' not in columns:
            connection.execute("ALTER TABLE resellers ADD COLUMN approved_by TEXT DEFAULT 'legacy-migration'")
        connection.execute('''CREATE TABLE IF NOT EXISTS reseller_interface_jobs (
            id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES resellers(id),
            child_id INTEGER NOT NULL REFERENCES resellers(id), port INTEGER NOT NULL
                CHECK(port BETWEEN 20000 AND 65535),
            state TEXT NOT NULL CHECK(state IN ('pending','complete','uncertain','failed')),
            iface_id INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            detail TEXT NOT NULL DEFAULT '')''')
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS reseller_interface_child_live
            ON reseller_interface_jobs(child_id) WHERE state IN ('pending','complete','uncertain')""")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS reseller_interface_port_live
            ON reseller_interface_jobs(port) WHERE state IN ('pending','complete','uncertain')""")
        connection.execute('CREATE INDEX IF NOT EXISTS reseller_parent_idx ON resellers(parent_id)')
        connection.execute('PRAGMA user_version=4')
        connection.commit()

    @staticmethod
    def public(row):
        result = dict(row)
        result.pop('password_hash', None)
        result['interfaces'] = json.loads(result['interfaces'])
        result['enabled'] = bool(result['enabled'])
        result['active'] = (result['enabled'] and result['expires_at'] > int(time.time())
                            and result.get('approval_status') == 'approved')
        result.setdefault('parent_id', None)
        result.setdefault('creation_fee_toman', 0)
        return result

    @classmethod
    def _public(cls, connection, row, *, rows=None, children=None):
        """Public hierarchy state from the same connection/transaction snapshot."""
        if rows is None:
            rows = {item['id']: item for item in connection.execute('SELECT * FROM resellers')}
        if children is None:
            children = {}
            for item in rows.values():
                children.setdefault(item['parent_id'], []).append(item['id'])
        result = cls.public(row)
        parent = rows.get(row['parent_id'])
        result['parent_username'] = parent['username'] if parent else None
        job = connection.execute('''SELECT id,parent_id,child_id,port,state,iface_id,created_at,updated_at
            FROM reseller_interface_jobs WHERE child_id=? ORDER BY id DESC LIMIT 1''', (row['id'],)).fetchone()
        result['interface_provision'] = dict(job) if job else None
        result['direct_child_count'] = len(children.get(row['id'], ()))
        pending, descendants = list(children.get(row['id'], ())), set()
        while pending:
            child_id = pending.pop()
            if child_id in descendants or child_id == row['id']:
                continue
            descendants.add(child_id)
            pending.extend(children.get(child_id, ()))
        result['descendant_count'] = len(descendants)
        seen, parent_id = {row['id']}, row['parent_id']
        now = int(time.time())
        while parent_id is not None:
            ancestor = rows.get(parent_id)
            if parent_id in seen or ancestor is None:
                result['active'] = False  # Corrupt/dangling ancestry fails closed.
                break
            seen.add(parent_id)
            if (not ancestor['enabled'] or ancestor['expires_at'] <= now
                    or ancestor['approval_status'] != 'approved'):
                result['active'] = False
            parent_id = ancestor['parent_id']
        return result

    def get(self, reseller_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            return self._public(db, row) if row else None

    def all(self):
        with self.db() as db:
            rows = {row['id']: row for row in db.execute('SELECT * FROM resellers ORDER BY id DESC')}
            children = {}
            for row in rows.values():
                children.setdefault(row['parent_id'], []).append(row['id'])
            return [self._public(db, row, rows=rows, children=children) for row in rows.values()]

    def children(self, parent_id):
        with self.db() as db:
            rows = {row['id']: row for row in db.execute('SELECT * FROM resellers ORDER BY id DESC')}
            children = {}
            for row in rows.values():
                children.setdefault(row['parent_id'], []).append(row['id'])
            return [self._public(db, row, rows=rows, children=children)
                    for row in rows.values() if row['parent_id'] == parent_id]

    def summary(self):
        with self.db() as db:
            row = db.execute('''SELECT count(*) AS total_resellers,
                coalesce(sum(CASE WHEN parent_id IS NOT NULL THEN 1 ELSE 0 END),0) AS subresellers,
                coalesce(sum(creation_fee_toman),0) AS total_creation_fee_toman,
                coalesce(sum(CASE WHEN approval_status!='approved' THEN 1 ELSE 0 END),0)
                    AS pending_approvals FROM resellers''').fetchone()
            return dict(row)

    def create(self, username, password, interfaces, max_peers=100, actor='admin', *,
               parent_id=None, creation_fee_toman=0, allow_unassigned=False):
        username = username.strip()
        if not 1 <= len(username) <= 64 or any(ord(c) < 32 for c in username):
            raise StoreError('نام کاربری نامعتبر است.')
        if not 12 <= len(password) <= 1024:
            raise StoreError('رمز باید بین ۱۲ تا ۱۰۲۴ نویسه باشد.')
        try:
            interfaces = sorted(set(int(i) for i in interfaces))
        except (TypeError, ValueError) as exc:
            raise StoreError('Interface و سقف مشتری را بررسی کنید.') from exc
        if (type(allow_unassigned) is not bool or
                (not interfaces and not (allow_unassigned and parent_id is not None)) or
                any(i < 1 for i in interfaces) or type(max_peers) is not int or not 1 <= max_peers <= 10000):
            raise StoreError('Interface و سقف مشتری را بررسی کنید.')
        if parent_id is not None and (type(parent_id) is not int or parent_id < 1):
            raise StoreError('نمایندهٔ والد نامعتبر است.')
        if type(creation_fee_toman) is not int or not 0 <= creation_fee_toman <= 10**15:
            raise StoreError('هزینهٔ ثبت نمایندگی نامعتبر است.')
        hashed = generate_password_hash(password)
        try:
            with self.db(write=True) as db:
                if parent_id is not None:
                    parent = db.execute('SELECT * FROM resellers WHERE id=?', (parent_id,)).fetchone()
                    if not parent or not self._public(db, parent)['active']:
                        raise StoreError('نمایندهٔ والد فعال نیست یا اعتبار آن پایان یافته است.')
                    if not set(interfaces).issubset(json.loads(parent['interfaces'])) or max_peers > parent['max_peers']:
                        raise StoreError('دسترسی و سقف زیرنماینده نمی‌تواند از نمایندهٔ والد بیشتر باشد.')
                cursor = db.execute('''INSERT INTO resellers
                    (username,password_hash,interfaces,max_peers,created_at,parent_id,creation_fee_toman,
                     approval_status,approved_at,approved_by)
                    VALUES(?,?,?,?,?,?,?,'pending',NULL,NULL)''',
                    (username, hashed, json.dumps(interfaces), max_peers, int(time.time()), parent_id, creation_fee_toman))
                reseller_id = cursor.lastrowid
                db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                           (reseller_id, 'created', actor, int(time.time())))
        except sqlite3.IntegrityError as exc:
            raise StoreError('این نام کاربری قبلاً ثبت شده است.') from exc
        return self.get(reseller_id)

    def approve(self, reseller_id, actor='admin', *, approved=True, days=30):
        """Mother-admin approval; callers must enforce the native admin boundary.

        approved_at records the first grant and survives revocation, preventing
        repeated approval or a revoke/reapprove cycle from adding free time.
        """
        if type(approved) is not bool or type(days) is not int or not 1 <= days <= 366:
            raise StoreError('وضعیت تأیید یا مدت اعتبار نامعتبر است.')
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            if not row:
                raise StoreError('نماینده پیدا نشد.')
            if approved and len(json.loads(row['interfaces'])) != 1:
                raise StoreError('قبل از تأیید، یک Interface اختصاصی برای نماینده تعیین کنید.')
            if approved:
                now = int(time.time())
                expires = row['expires_at']
                if row['approved_at'] is None:
                    expires = max(now, expires) + days * 86400
                db.execute("""UPDATE resellers SET approval_status='approved',
                    approved_at=coalesce(approved_at,?),approved_by=?,expires_at=? WHERE id=?""",
                    (now, actor, expires, reseller_id))
            else:
                db.execute("UPDATE resellers SET approval_status='pending' WHERE id=?", (reseller_id,))
            db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                       (reseller_id, 'approval:approved' if approved else 'approval:revoked', actor, int(time.time())))
        return self.get(reseller_id)

    @classmethod
    def _provision_child(cls, db, parent_id, child_id):
        if type(parent_id) is not int or type(child_id) is not int or min(parent_id, child_id) < 1:
            raise StoreError('نماینده یا زیرنماینده نامعتبر است.')
        parent = db.execute('SELECT * FROM resellers WHERE id=?', (parent_id,)).fetchone()
        child = db.execute('SELECT * FROM resellers WHERE id=?', (child_id,)).fetchone()
        if not parent or not cls._public(db, parent)['active']:
            raise StoreError('نمایندهٔ والد فعال نیست یا اعتبار آن پایان یافته است.')
        if not child or child['parent_id'] != parent_id:
            raise StoreError('زیرنماینده متعلق به این نماینده نیست.')
        if child['approval_status'] != 'pending' or json.loads(child['interfaces']):
            raise StoreError('Interface فقط برای زیرنمایندهٔ جدید و در انتظار تأیید ساخته می‌شود.')
        return child

    def reserve_interface(self, parent_id, child_id, port):
        if type(port) is not int or not 20000 <= port <= 65535:
            raise StoreError('پورت باید بین ۲۰۰۰۰ تا ۶۵۵۳۵ باشد.')
        try:
            with self.db(write=True) as db:
                self._provision_child(db, parent_id, child_id)
                now = int(time.time())
                cursor = db.execute('''INSERT INTO reseller_interface_jobs
                    (parent_id,child_id,port,state,created_at,updated_at) VALUES(?,?,?,'pending',?,?)''',
                    (parent_id, child_id, port, now, now))
                result = dict(db.execute('SELECT * FROM reseller_interface_jobs WHERE id=?',
                                         (cursor.lastrowid,)).fetchone())
                db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                           (child_id, 'interface:reserved', 'reseller:' + str(parent_id), now))
                return result
        except sqlite3.IntegrityError as exc:
            raise StoreError('پورت رزرو شده یا ساخت Interface این زیرنماینده قبلاً آغاز شده است؛ وضعیت را مدیر بررسی کند.') from exc

    def attach_child_interface(self, parent_id, child_id, iface_id, actor=None):
        if type(iface_id) is not int or iface_id < 1:
            raise StoreError('Interface نامعتبر است.')
        with self.db(write=True) as db:
            self._provision_child(db, parent_id, child_id)
            job = db.execute("""SELECT * FROM reseller_interface_jobs
                WHERE parent_id=? AND child_id=? AND state='pending'""", (parent_id, child_id)).fetchone()
            if not job:
                raise StoreError('رزرو معتبر برای اتصال Interface پیدا نشد.')
            # This dedicated assignment is intentionally separate from inherited
            # create() access. The backend alone supplies the native creation ID.
            for row in db.execute('SELECT id,interfaces FROM resellers WHERE id!=?', (child_id,)):
                if iface_id in json.loads(row['interfaces']):
                    raise StoreError('Interface به نمایندهٔ دیگری اختصاص داده شده است.')
            db.execute('UPDATE resellers SET interfaces=?,auth_version=auth_version+1 WHERE id=?',
                       (json.dumps([iface_id]), child_id))
            now = int(time.time())
            db.execute("UPDATE reseller_interface_jobs SET state='complete',iface_id=?,updated_at=? WHERE id=?",
                       (iface_id, now, job['id']))
            db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                       (child_id, 'interface:' + str(iface_id), actor or 'reseller:' + str(parent_id), now))
        return self.get(child_id)

    def fail_interface(self, parent_id, child_id, *, uncertain=True, detail=''):
        if type(uncertain) is not bool:
            raise StoreError('وضعیت ساخت Interface نامعتبر است.')
        with self.db(write=True) as db:
            job = db.execute("""SELECT * FROM reseller_interface_jobs
                WHERE parent_id=? AND child_id=? AND state='pending'""", (parent_id, child_id)).fetchone()
            if not job:
                raise StoreError('رزرو در حال ساخت پیدا نشد.')
            db.execute('UPDATE reseller_interface_jobs SET state=?,detail=?,updated_at=? WHERE id=?',
                       ('uncertain' if uncertain else 'failed', str(detail)[:500], int(time.time()), job['id']))
            return dict(db.execute('SELECT * FROM reseller_interface_jobs WHERE id=?', (job['id'],)).fetchone())

    def interface_reservations(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM reseller_interface_jobs
                WHERE state IN ('pending','complete','uncertain') ORDER BY id""")]

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
            return self._public(db, row), False

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
                        return self._public(db, row)
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

    def assign_interface(self, reseller_id, iface_id, actor='admin'):
        if type(iface_id) is not int or iface_id < 1:
            raise StoreError('Interface نامعتبر است.')
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            if not row:
                raise StoreError('نماینده پیدا نشد.')
            db.execute('UPDATE resellers SET interfaces=?,auth_version=auth_version+1 WHERE id=?',
                       (json.dumps([iface_id]), reseller_id))
            db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)',
                       (reseller_id, 'interface:' + str(iface_id), actor, int(time.time())))
        return self.get(reseller_id)

    def ownership(self, reseller_id):
        with self.db() as db:
            return {row['peer_id']: row['public_key'] for row in db.execute(
                'SELECT peer_id,public_key FROM reseller_peers WHERE reseller_id=?', (reseller_id,))}

    def reserve(self, reseller_id, request_id):
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM resellers WHERE id=?', (reseller_id,)).fetchone()
            if not row or not self._public(db, row)['active']:
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
