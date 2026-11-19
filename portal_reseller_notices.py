"""Additive dashboard announcements, independent of the reseller schema version."""
import hashlib
import json
import re
import time
import unicodedata
class StoreError(ValueError):
    """Transport-neutral validation error; independent of store import paths."""


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS reseller_dashboard_notices (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, actor TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS reseller_notice_receipts (actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, notice_id INTEGER NOT NULL REFERENCES reseller_dashboard_notices(id), UNIQUE(actor,request_id))')


def _public(row):
    value = dict(row)
    value['active'] = bool(value['active'])
    return value


def listing(store, *, public=False):
    with store.db(write=True) as db:
        _ensure(db)
        rows = db.execute('SELECT * FROM reseller_dashboard_notices ' + ('WHERE active=1 ' if public else '') + 'ORDER BY id DESC').fetchall()
    result = [_public(row) for row in rows]
    if public:
        for item in result:
            item.pop('actor', None)
    return result


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StoreError('عنوان و متن پیام الزامی هستند و باید در محدودهٔ مجاز باشند.')
    if any(unicodedata.category(c) == 'Cc' and c not in '\n\r\t' for c in value):
        raise StoreError('متن پیام دارای نویسهٔ کنترلی نامعتبر است.')
    return value.strip()


def create(store, value, actor, request_id):
    if set(value) != {'title', 'body'}:
        raise StoreError('فقط عنوان و متن پیام مجاز هستند.')
    title, body = _text(value['title'], 160), _text(value['body'], 8000)
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{16,80}', request_id):
        raise StoreError('شناسهٔ یکتای درخواست معتبر الزامی است.')
    fingerprint = hashlib.sha256(json.dumps([title, body], ensure_ascii=False).encode()).hexdigest()
    with store.db(write=True) as db:
        _ensure(db)
        previous = db.execute('SELECT * FROM reseller_notice_receipts WHERE actor=? AND request_id=?', (actor, request_id)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise StoreError('این شناسهٔ درخواست قبلاً برای پیام دیگری استفاده شده است.')
            return _public(db.execute('SELECT * FROM reseller_dashboard_notices WHERE id=?', (previous['notice_id'],)).fetchone())
        now = int(time.time())
        notice_id = db.execute('INSERT INTO reseller_dashboard_notices(title,body,created_at,actor) VALUES(?,?,?,?)', (title, body, now, actor)).lastrowid
        db.execute('INSERT INTO reseller_notice_receipts VALUES(?,?,?,?)', (actor, request_id, fingerprint, notice_id))
        db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(NULL,?,?,?)', ('notice:create:' + str(notice_id), actor, now))
        return _public(db.execute('SELECT * FROM reseller_dashboard_notices WHERE id=?', (notice_id,)).fetchone())


def update(store, notice_id, value, actor):
    if set(value) != {'active'} or type(value['active']) is not bool:
        raise StoreError('وضعیت نمایش پیام باید درست یا نادرست باشد.')
    with store.db(write=True) as db:
        _ensure(db)
        row = db.execute('SELECT * FROM reseller_dashboard_notices WHERE id=?', (notice_id,)).fetchone()
        if row is None:
            return None
        if bool(row['active']) != value['active']:
            db.execute('UPDATE reseller_dashboard_notices SET active=? WHERE id=?', (int(value['active']), notice_id))
            db.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(NULL,?,?,?)', ('notice:active:' + str(notice_id) + ':' + str(int(value['active'])), actor, int(time.time())))
        return _public(db.execute('SELECT * FROM reseller_dashboard_notices WHERE id=?', (notice_id,)).fetchone())
