"""Encrypted reseller TOTP and one-use recovery codes; no network operations."""
import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from flask import current_app
from werkzeug.security import check_password_hash


def cipher():
    from cryptography.fernet import Fernet
    secret = current_app.secret_key
    if not secret:
        raise ValueError('Panel security key is unavailable')
    secret = secret.encode() if isinstance(secret, str) else secret
    return Fernet(base64.urlsafe_b64encode(hmac.new(secret, b'wgportal-reseller-totp-v1', hashlib.sha256).digest()))


def normalized(value):
    return str(value or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')).strip().replace('-', '').replace(' ', '').lower()


def counter_for(secret, value, previous=-1, now=None):
    value = normalized(value)
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        return None
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    center = int(time.time() if now is None else now) // 30
    for counter in (center, center - 1, center + 1):
        if counter <= previous or counter < 0:
            continue
        digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
        offset = digest[-1] & 15
        number = (struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff) % 1000000
        if hmac.compare_digest(f'{number:06d}', value):
            return counter
    return None


def recovery_hash(account_id, value):
    return hashlib.sha256(('wgportal-recovery:' + str(account_id) + ':' + normalized(value)).encode()).hexdigest()


def verify_second_factor(connection, account_id, value):
    row = connection.execute('SELECT * FROM reseller_security WHERE reseller_id=?', (account_id,)).fetchone()
    if not row or not row['secret_enc']:
        return True
    secret = cipher().decrypt(row['secret_enc'].encode()).decode()
    counter = counter_for(secret, value, row['last_counter'])
    if counter is not None:
        connection.execute('UPDATE reseller_security SET last_counter=? WHERE reseller_id=?', (counter, account_id))
        return True
    digest = recovery_hash(account_id, value)
    codes = json.loads(row['recovery_hashes'])
    matching = next((code for code in codes if hmac.compare_digest(code, digest)), None)
    if matching:
        codes.remove(matching)
        connection.execute('UPDATE reseller_security SET recovery_hashes=? WHERE reseller_id=?', (json.dumps(codes), account_id))
        return True
    return False


def status(store, account_id):
    with store.db() as connection:
        row = connection.execute('SELECT * FROM reseller_security WHERE reseller_id=?', (account_id,)).fetchone()
        return {'enabled': bool(row and row['secret_enc']),
                'recovery_codes_remaining': len(json.loads(row['recovery_hashes'])) if row else 0}


def reauthenticate(store, account_id, password, identity):
    from deploy.linux.reseller_store import StoreError
    now = int(time.time())
    bucket = str(account_id) + ':' + str(identity or '')[:128]
    accepted = False
    limited = False
    with store.db(write=True) as connection:
        connection.execute('DELETE FROM reseller_security_attempts WHERE created_at<?', (now-900,))
        limited = connection.execute('SELECT count(*) FROM reseller_security_attempts WHERE reseller_id=?', (account_id,)).fetchone()[0] >= 20
        if not limited:
            connection.execute('INSERT INTO reseller_security_attempts VALUES(?,?,?)', (account_id, bucket, now))
            row = connection.execute('SELECT * FROM resellers WHERE id=?', (account_id,)).fetchone()
            accepted = bool(row and not row['deleted_at'] and isinstance(password, str) and len(password) <= 1024 and check_password_hash(row['password_hash'], password))
    if limited:
        raise StoreError('تلاش‌های امنیتی زیاد است؛ پانزده دقیقه بعد دوباره تلاش کنید.')
    if not accepted:
        raise StoreError('رمز فعلی درست نیست.')


def setup(store, account_id, password, identity):
    from deploy.linux.reseller_store import StoreError
    reauthenticate(store, account_id, password, identity)
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
    encrypted = cipher().encrypt(secret.encode()).decode()
    with store.db(write=True) as connection:
        account = connection.execute('SELECT * FROM resellers WHERE id=?', (account_id,)).fetchone()
        if not account or account['deleted_at']:
            raise StoreError('حساب در دسترس نیست.')
        row = connection.execute('SELECT secret_enc FROM reseller_security WHERE reseller_id=?', (account_id,)).fetchone()
        if row and row['secret_enc']:
            raise StoreError('ورود دومرحله‌ای فعال است؛ ابتدا با کد فعلی غیرفعال کنید.')
        connection.execute("INSERT INTO reseller_security(reseller_id) VALUES(?) ON CONFLICT(reseller_id) DO NOTHING", (account_id,))
        connection.execute('UPDATE reseller_security SET pending_enc=?,pending_until=? WHERE reseller_id=?', (encrypted, int(time.time())+600, account_id))
        username = account['username']
    uri = 'otpauth://totp/' + quote('WG Portal:' + username, safe='') + '?' + urlencode({'secret': secret, 'issuer': 'WG Portal', 'algorithm': 'SHA1', 'digits': 6, 'period': 30})
    qr = None
    try:
        import qrcode
        output = io.BytesIO()
        qrcode.make(uri).save(output, format='PNG')
        qr = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()
    except ImportError:
        pass  # Manual entry remains available; no third-party QR service.
    return {'secret': secret, 'otpauth_uri': uri, 'qr_data_uri': qr, 'expires_in': 600}


def change(store, account_id, password, otp, identity, *, enable):
    from deploy.linux.reseller_store import StoreError
    reauthenticate(store, account_id, password, identity)
    recovery = None
    with store.db(write=True) as connection:
        account = connection.execute('SELECT * FROM resellers WHERE id=?', (account_id,)).fetchone()
        row = connection.execute('SELECT * FROM reseller_security WHERE reseller_id=?', (account_id,)).fetchone()
        if not account or account['deleted_at'] or row is None:
            raise StoreError('تنظیم دومرحله‌ای در دسترس نیست.')
        if enable:
            if row['secret_enc'] or not row['pending_enc'] or row['pending_until'] <= int(time.time()):
                raise StoreError('درخواست راه‌اندازی منقضی یا قبلاً استفاده شده است.')
            secret = cipher().decrypt(row['pending_enc'].encode()).decode()
            counter = counter_for(secret, otp)
            if counter is None:
                raise StoreError('کد تأیید درست نیست؛ کد تازهٔ برنامه را وارد کنید.')
            recovery = [secrets.token_hex(12) for _ in range(10)]
            hashes = json.dumps([recovery_hash(account_id, value) for value in recovery])
            connection.execute("UPDATE reseller_security SET secret_enc=pending_enc,pending_enc='',pending_until=0,last_counter=?,recovery_hashes=? WHERE reseller_id=?", (counter, hashes, account_id))
        else:
            if not row['secret_enc'] or not verify_second_factor(connection, account_id, otp):
                raise StoreError('کد دومرحله‌ای یا بازیابی درست نیست.')
            connection.execute("UPDATE reseller_security SET secret_enc='',pending_enc='',pending_until=0,last_counter=-1,recovery_hashes='[]' WHERE reseller_id=?", (account_id,))
        connection.execute('UPDATE resellers SET auth_version=auth_version+1 WHERE id=?', (account_id,))
        connection.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)', (account_id, '2fa:enabled' if enable else '2fa:disabled', 'reseller:'+str(account_id), int(time.time())))
    return {'enabled': enable, **({'recovery_codes': recovery} if recovery is not None else {})}


def reset(store, account_id, actor, request_id=None):
    from deploy.linux.reseller_store import StoreError
    if request_id is not None and (not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id)):
        raise StoreError('شناسهٔ یکتای بازنشانی الزامی است.')
    replay = False
    with store.db(write=True) as connection:
        account = connection.execute('SELECT * FROM resellers WHERE id=?', (account_id,)).fetchone()
        if not account or account['deleted_at']:
            raise StoreError('نماینده پیدا نشد.')
        if request_id is not None:
            receipt = connection.execute('SELECT actor FROM reseller_security_resets WHERE reseller_id=? AND request_id=?',
                (account_id,request_id)).fetchone()
            if receipt:
                if receipt['actor'] != actor:
                    raise StoreError('شناسهٔ بازنشانی قبلاً توسط مدیر دیگری استفاده شده است.')
                replay = True
        if not replay:
            connection.execute('DELETE FROM reseller_security WHERE reseller_id=?', (account_id,))
            connection.execute('UPDATE resellers SET auth_version=auth_version+1 WHERE id=?', (account_id,))
            now = int(time.time())
            connection.execute('INSERT INTO reseller_audit(reseller_id,action,actor,created_at) VALUES(?,?,?,?)', (account_id, '2fa:reset', actor, now))
            if request_id is not None:
                connection.execute('INSERT INTO reseller_security_resets(reseller_id,request_id,actor,created_at) VALUES(?,?,?,?)',
                    (account_id,request_id,actor,now))
    return store.get(account_id)
