"""Scoped reseller extension around native WireGuard operations.

Reseller sessions never become WG_Panel administrator sessions. Only explicit
operations below use the server-side native API credential.
"""
from functools import wraps
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import quote, urlsplit

import requests
from flask import Blueprint, Response, abort, current_app, g, jsonify, request, send_from_directory, session

if __package__:
    from .reseller_store import ResellerStore, StoreError
else:
    from reseller_store import ResellerStore, StoreError


class CoreError(Exception):
    pass


class NativeCore:
    def __init__(self, url, api_key):
        self.url, self.key = url.rstrip('/'), api_key

    def call(self, method, path, payload=None, *, cookie=None):
        headers = {'Accept': 'application/json'}
        if cookie is not None:
            headers['Cookie'] = cookie
        elif self.key:
            headers['X-API-KEY'] = self.key
        else:
            raise CoreError('Native API credential is not configured')
        with requests.Session() as client:
            client.trust_env = False
            try:
                with client.request(method, self.url + path, json=payload, headers=headers,
                                    timeout=(3, 20), stream=True, allow_redirects=False) as result:
                    body = bytearray()
                    for chunk in result.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 16 * 1024 * 1024:
                            raise CoreError('Native response exceeds safe size')
                    return result.status_code, bytes(body), result.headers.get('Content-Type', '')
            except requests.RequestException as exc:
                raise CoreError('Native panel is unavailable; do not repeat uncertain creates') from exc

    def json(self, method, path, payload=None, *, cookie=None):
        code, body, _ = self.call(method, path, payload, cookie=cookie)
        try:
            return code, json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise CoreError('Native API returned an unexpected response') from exc


def reseller_blueprint(panel_url, database, api_key, *, core=None,
                       owner_cookie_reader=None, native_scheme=False):
    bp = Blueprint('reseller', __name__)
    store = ResellerStore(database)
    core = core if core is not None else NativeCore(panel_url, api_key)
    ui = Path(__file__).parent / 'reseller_ui'

    def native():
        return current_app.config.get('RESELLER_CORE', core)

    def csrf():
        if not session.get('reseller_csrf'):
            session['reseller_csrf'] = secrets.token_urlsafe(32)
        return session['reseller_csrf']

    def active_account():
        value = session.get('reseller_identity') or {}
        if not isinstance(value, dict):
            return None
        account = store.get(value.get('id', 0))
        if not account or account['auth_version'] != value.get('version'):
            return None
        return account

    def owner():
        # Only native browser cookies establish administrator authority here.
        # In particular, the reseller cookie and API key are never used.
        cookies = owner_cookie_reader() if owner_cookie_reader is not None else '; '.join(
            name + '=' + request.cookies[name]
            for name in ('session', 'remember_token') if request.cookies.get(name))
        if not cookies:
            return None
        try:
            status, data = native().json('GET', '/api/admin', cookie=cookies)
            return data.get('username') if status == 200 and isinstance(data, dict) else None
        except CoreError:
            return None

    def require_owner(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            g.reseller_owner = owner()
            if not g.reseller_owner:
                return jsonify(error='admin_required', message='با حساب مدیر مادر وارد شوید.'), 401
            return fn(*args, **kwargs)
        return wrapped

    def require_reseller(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            g.reseller = active_account()
            if not g.reseller:
                return jsonify(error='login_required'), 401
            if not g.reseller['active']:
                return jsonify(error='panel_paused', message='اعتبار پنل پایان یافته یا دسترسی متوقف شده است.'), 403
            return fn(*args, **kwargs)
        return wrapped

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400)
        return value

    def interfaces():
        code, value = native().json('GET', '/api/get-interfaces')
        if code != 200 or not isinstance(value, dict):
            raise CoreError('Native interfaces unavailable')
        return value.get('interfaces', [])

    def owned_peers(account):
        code, value = native().json('GET', '/api/peers')
        if code != 200 or not isinstance(value, dict):
            raise CoreError('Native peers unavailable')
        ownership = store.ownership(account['id'])
        # Match both id and public key: a deleted/reused native id cannot grant access.
        return [peer for peer in value.get('peers', [])
                if peer.get('id') in ownership and peer.get('public_key') == ownership[peer['id']]
                and peer.get('iface_id') in account['interfaces']]

    def owned_peer(account, peer_id):
        peer = next((p for p in owned_peers(account) if p['id'] == peer_id), None)
        if not peer:
            abort(404)
        return peer

    @bp.before_request
    def protect():
        if not (request.path == '/reseller' or request.path.startswith('/reseller/') or
                request.path == '/resellers' or request.path.startswith('/resellers/')):
            return None
        request.max_content_length = 32768
        if request.content_length and request.content_length > 32768:
            abort(413)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = urlsplit(request.headers.get('Origin', ''))
            # Embedded mode follows the native panel's own HTTP/TLS choice.
            # Forwarded headers never change this decision.
            scheme = request.scheme if native_scheme else 'https'
            if scheme not in {'http', 'https'} or origin.scheme != scheme or origin.netloc.lower() != request.host.lower():
                abort(403)
            expected = session.get('reseller_csrf', '')
            supplied = request.headers.get('X-Reseller-CSRF', '')
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                abort(403)

    @bp.after_request
    def headers(result):
        if request.path.startswith('/reseller'):
            result.headers['Cache-Control'] = 'no-store'
            result.headers['X-Content-Type-Options'] = 'nosniff'
            result.headers['Referrer-Policy'] = 'no-referrer'
            result.headers['X-Frame-Options'] = 'DENY'
            result.headers['Content-Security-Policy'] = ("default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                "object-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:")
        return result

    @bp.errorhandler(CoreError)
    def core_failure(_error):
        return jsonify(error='native_unavailable', message='عملیات پنل اصلی کامل نشد؛ وضعیت را بررسی کنید.'), 502

    @bp.errorhandler(StoreError)
    def store_failure(error):
        return jsonify(error='invalid_request', message=str(error)), 400

    @bp.get('/reseller')
    @bp.get('/resellers')
    def page():
        return send_from_directory(ui, 'index.html')

    @bp.get('/reseller/assets/<name>')
    def assets(name):
        if name not in {'reseller.js', 'reseller.css'}:
            abort(404)
        return send_from_directory(ui, name)

    @bp.get('/reseller/api/session')
    def status():
        return jsonify(csrf=csrf(), account=active_account())

    @bp.post('/reseller/api/login')
    def login():
        value = data()
        username, password = str(value.get('username', '')), str(value.get('password', ''))
        if len(username) > 64 or len(password) > 1024:
            return jsonify(error='invalid_credentials'), 401
        address = request.remote_addr or ''
        try:
            if ipaddress.ip_address(address).is_loopback:
                address = str(ipaddress.ip_address(request.headers.get('X-Forwarded-For', '').split(',')[-1].strip()))
        except ValueError:
            pass
        account, limited = store.authenticate(username, password, address)
        if limited:
            return jsonify(error='rate_limited', message='کمی بعد دوباره تلاش کنید.'), 429
        if not account:
            return jsonify(error='invalid_credentials', message='نام کاربری یا رمز درست نیست.'), 401
        session.clear()
        session.permanent = True
        session['reseller_identity'] = {'id': account['id'], 'version': account['auth_version']}
        return jsonify(ok=True, account=account, csrf=csrf())

    @bp.post('/reseller/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @bp.get('/resellers/api/accounts')
    @require_owner
    def accounts():
        return jsonify(accounts=store.all(), interfaces=interfaces(), audit=store.audit(), operations=store.operations(), csrf=csrf())

    @bp.post('/resellers/api/accounts')
    @require_owner
    def create_account():
        value = data()
        selected = value.get('interfaces', [])
        if not isinstance(selected, list) or any(type(i) is not int for i in selected):
            raise StoreError('شناسهٔ Interface نامعتبر است.')
        allowed = {row['id'] for row in interfaces()}
        if not set(selected) <= allowed:
            raise StoreError('Interface انتخاب‌شده وجود ندارد.')
        capacity = value.get('max_peers', 100)
        if type(capacity) is not int:
            raise StoreError('ظرفیت نامعتبر است.')
        account = store.create(str(value.get('username', '')), str(value.get('password', '')),
                               selected, capacity, g.reseller_owner)
        return jsonify(account=account), 201

    @bp.post('/resellers/api/accounts/<int:account_id>/renew')
    @require_owner
    def renew(account_id):
        request_id = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise StoreError('شناسهٔ یکتای تمدید الزامی است.')
        return jsonify(account=store.update(account_id, days=data().get('days'), actor=g.reseller_owner, request_id=request_id))

    @bp.post('/resellers/api/accounts/<int:account_id>/access')
    @require_owner
    def access(account_id):
        return jsonify(account=store.update(account_id, enabled=data().get('enabled'), actor=g.reseller_owner))

    @bp.get('/reseller/api/peers')
    @require_reseller
    def peers():
        fields = {'id','name','iface','status','panel_status','conn_status','used_bytes','data_limit','limit_unit','ttl_seconds'}
        return jsonify(peers=[{k: v for k, v in p.items() if k in fields} for p in owned_peers(g.reseller)],
                       interfaces=[{'id': i['id'], 'name': i['name']} for i in interfaces() if i['id'] in g.reseller['interfaces']])

    @bp.post('/reseller/api/peers')
    @require_reseller
    def create_peer():
        value = data()
        iface_id = value.get('iface_id')
        if type(iface_id) is not int or iface_id not in g.reseller['interfaces']:
            abort(403)
        name = str(value.get('name', '')).strip()
        days, volume = value.get('days', 30), value.get('volume_gb', 50)
        if not 1 <= len(name) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in name) or type(days) is not int or not 1 <= days <= 366 or type(volume) is not int or not 1 <= volume <= 100000:
            raise StoreError('نام مشتری، حجم یا زمان نامعتبر است.')
        request_id = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise StoreError('شناسهٔ یکتای درخواست الزامی است.')
        operation = store.reserve(g.reseller['id'], request_id)
        if operation['state'] != 'new':
            if operation['state'] == 'complete':
                return jsonify(ok=True, id=operation['peer_id'], replay=True)
            return jsonify(error='operation_not_repeatable', message='وضعیت این درخواست نیازمند بررسی مدیر است.'), 409
        try:
            code, result = native().json('POST', '/api/peers', {'iface_id': iface_id, 'scope': 'local', 'name': name,
                'time_limit_days': days, 'data_limit_value': volume, 'data_limit_unit': 'Gi', 'unlimited': False})
            if code != 200 or not isinstance(result, dict) or not result.get('id') or not result.get('public_key'):
                store.failed(operation['id'], uncertain=True)
                return jsonify(error='native_create_failed', message='ساخت تأیید نشد؛ پیش از تلاش جدید مدیر بررسی کند.'), 502
            store.finish(operation['id'], g.reseller['id'], int(result['id']), str(result['public_key']))
            return jsonify(ok=True, id=result['id']), 201
        except Exception:
            store.failed(operation['id'], uncertain=True)
            return jsonify(error='create_uncertain', message='نتیجهٔ ساخت نامشخص است؛ این درخواست را تکرار نکنید و به مدیر اطلاع دهید.'), 502

    @bp.post('/reseller/api/peers/<int:peer_id>/<action>')
    @require_reseller
    def peer_action(peer_id, action):
        if action not in {'enable', 'disable'}:
            abort(404)
        peer = owned_peer(g.reseller, peer_id)
        # Native id-based mutators have a TOCTOU boundary if another admin deletes
        # and recreates the same id. Use the atomic key-checked product endpoint.
        code, result = native().json('POST', '/api/portal/owned-peer-action',
                                    {'id': peer_id, 'public_key': store.ownership(g.reseller['id'])[peer_id], 'action': action})
        if code != 200:
            return jsonify(error='native_action_failed'), 409
        return jsonify(ok=True)

    @bp.get('/reseller/api/peers/<int:peer_id>/<kind>')
    @require_reseller
    def delivery(peer_id, kind):
        if kind not in {'config', 'qr'}:
            abort(404)
        owned_peer(g.reseller, peer_id)
        public_key = store.ownership(g.reseller['id'])[peer_id]
        path = '/api/peer/' + quote(public_key, safe='') + ('/config_qr' if kind == 'qr' else '/config?download=1')
        code, body, content_type = native().call('GET', path)
        if code != 200:
            return jsonify(error='delivery_unavailable'), 502
        result = Response(body, content_type='image/png' if kind == 'qr' else 'text/plain; charset=utf-8')
        if kind == 'config':
            result.headers['Content-Disposition'] = f'attachment; filename="peer-{peer_id}.conf"'
        return result

    return bp
