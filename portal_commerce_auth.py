"""Telegram Mini App authentication, independent of administrator cookies.

HMAC contract: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
Only hashed, bounded-lifetime bearer credentials are persisted.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    def __init__(self, code='invalid_telegram_auth'):
        self.code=code;self.status=401;self.message='ورود تلگرام معتبر نیست یا منقضی شده است؛ پنل را دوباره از بات باز کنید.'
        super().__init__(code)


def _fields(raw):
    if not isinstance(raw,str) or not 1<=len(raw)<=16384 or re.search(r'%(?![0-9a-fA-F]{2})',raw):
        raise AuthenticationError()
    try:
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True,encoding='utf-8',errors='strict',max_num_fields=32)
    except (ValueError,UnicodeError):raise AuthenticationError() from None
    fields={}
    for key,value in pairs:
        if key in fields or not re.fullmatch(r'[a-z_]{1,64}',key):raise AuthenticationError()
        fields[key]=value
    return fields


def validate_init_data(raw,token,now=None,max_age=300):
    fields=_fields(raw)
    received=fields.pop('hash',None)
    if not isinstance(token,str) or not token or not isinstance(received,str) or not re.fullmatch(r'[a-fA-F0-9]{64}',received):
        raise AuthenticationError()
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    calculated=hmac.new(secret,'\n'.join(key+'='+fields[key] for key in sorted(fields)).encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated,received.lower()):raise AuthenticationError()
    stamp=fields.get('auth_date','')
    if not re.fullmatch(r'[0-9]{1,12}',stamp):raise AuthenticationError()
    now=int(time.time() if now is None else now)
    if int(stamp)>now+30 or now-int(stamp)>max_age:raise AuthenticationError('expired_telegram_auth')
    try:user=json.loads(fields.get('user',''))
    except (ValueError,TypeError):raise AuthenticationError() from None
    if not isinstance(user,dict) or type(user.get('id')) is not int or not 0<user['id']<2**52 or user.get('is_bot'):
        raise AuthenticationError()
    identity={'id':user['id']}
    for name in ('first_name','last_name','username','language_code'):
        value=user.get(name,'')
        if not isinstance(value,str) or len(value)>256:raise AuthenticationError()
        identity[name]=value
    return identity


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS commerce_auth_replays (fingerprint TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS commerce_auth_sessions (token_hash TEXT PRIMARY KEY, customer_id INTEGER NOT NULL, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL, bot_fingerprint TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS commerce_auth_session_expiry ON commerce_auth_sessions(expires_at)')


class Auth:
    def __init__(self,store,token_provider,service,*,clock=None,lifetime=1800,max_age=300):
        self.store,self.token_provider,self.service=store,token_provider,service
        self.clock=clock or time.time;self.lifetime=lifetime;self.max_age=max_age

    def login(self,raw):
        token=self.token_provider()
        user=validate_init_data(raw,token,now=self.clock(),max_age=self.max_age)
        fields=_fields(raw)
        fingerprint=hashlib.sha256((str(token).split(':')[0]+':'+fields['hash'].lower()).encode()).hexdigest()
        now=int(self.clock())
        with self.store.db(write=True) as db:
            _ensure(db)
            if db.execute('SELECT 1 FROM commerce_auth_replays WHERE fingerprint=?',(fingerprint,)).fetchone():
                raise AuthenticationError('telegram_auth_replayed')
        referral=fields.get('start_param','')
        referral=referral[4:] if referral.startswith('ref_') and re.fullmatch(r'ref_[A-Za-z0-9_-]{4,80}',referral) else None
        customer=self.service.customer_from_telegram(user,referral_code=referral)
        if not isinstance(customer,dict) or type(customer.get('id')) is not int or customer.get('suspended'):
            raise AuthenticationError('customer_suspended')
        bearer=secrets.token_urlsafe(48);csrf=secrets.token_urlsafe(32)
        bot_fingerprint=hashlib.sha256(token.encode()).hexdigest()
        try:
            with self.store.db(write=True) as db:
                _ensure(db)
                db.execute('DELETE FROM commerce_auth_replays WHERE expires_at<?',(now,))
                db.execute('DELETE FROM commerce_auth_sessions WHERE expires_at<?',(now,))
                db.execute('INSERT INTO commerce_auth_replays VALUES(?,?)',(fingerprint,now+self.max_age+60))
                db.execute('INSERT INTO commerce_auth_sessions VALUES(?,?,?,?,?)',(hashlib.sha256(bearer.encode()).hexdigest(),customer['id'],csrf,now+self.lifetime,bot_fingerprint))
        except sqlite3.IntegrityError:raise AuthenticationError('telegram_auth_replayed') from None
        return {'token':bearer,'csrf':csrf,'customer':customer,'expires_at':now+self.lifetime}

    def authenticate(self,rawtoken):
        if not isinstance(rawtoken,str) or not re.fullmatch(r'[A-Za-z0-9_-]{64}',rawtoken):raise AuthenticationError('customer_login_required')
        token=self.token_provider()
        with self.store.db(write=True) as db:
            _ensure(db)
            row=db.execute('SELECT * FROM commerce_auth_sessions WHERE token_hash=?',(hashlib.sha256(rawtoken.encode()).hexdigest(),)).fetchone()
            customer=db.execute('SELECT id,telegram_id,first_name,username,suspended,referral_code FROM customers WHERE id=?',(row['customer_id'],)).fetchone() if row else None
        if not row or row['expires_at']<=int(self.clock()) or not token or not hmac.compare_digest(row['bot_fingerprint'],hashlib.sha256(token.encode()).hexdigest()):
            raise AuthenticationError('customer_login_required')
        if not customer or customer['suspended']:raise AuthenticationError('customer_suspended')
        return {'customer_id':row['customer_id'],'csrf':row['csrf'],'expires_at':row['expires_at'],'customer':dict(customer)}

    def logout(self,rawtoken):
        if isinstance(rawtoken,str):
            with self.store.db(write=True) as db:
                _ensure(db)
                db.execute('DELETE FROM commerce_auth_sessions WHERE token_hash=?',(hashlib.sha256(rawtoken.encode()).hexdigest(),))
