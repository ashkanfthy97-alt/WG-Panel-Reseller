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
from flask import Blueprint, Response, abort, current_app, g, jsonify, redirect, request, send_from_directory, session

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

    def account_details(accounts, rows=None):
        rows = interfaces() if rows is None else rows
        safe = {row['id']: {key: row.get(key) for key in ('id', 'name', 'listen_port', 'address')}
                for row in rows}
        return [{**account, 'assigned_interfaces': [safe[i] for i in account['interfaces'] if i in safe]}
                for account in accounts]

    def check_username(value):
        lookup = current_app.extensions.get('portal_native_admin_lookup')
        if lookup is not None and lookup(str(value or '').strip()):
            raise StoreError('این نام کاربری قبلاً ثبت شده است.')

    def child_for_provision(child_id):
        child = store.get(child_id)
        if not child or child.get('parent_id') != g.reseller['id']:
            abort(404)
        if child['interfaces'] or child.get('approval_status') == 'approved' or child.get('interface_provision'):
            raise StoreError('این زیرنماینده قبلاً Interface دارد یا ساخت آن نیازمند بررسی مدیر است.')
        return child

    def port_snapshot(rows):
        from portal_provisioning import occupied_ports
        return occupied_ports(rows, current_app.config.get('WG_CONF_PATH') or '/etc/wireguard',
                              store.interface_reservations())

    def owned_peers(account):
        code, value = native().json('GET', '/api/peers')
        if code != 200 or not isinstance(value, dict):
            raise CoreError('Native peers unavailable')
        ownership = store.ownership(account['id'])
        # Match both id and public key: a deleted/reused native id cannot grant access.
        return [peer for peer in value.get('peers', [])
                if peer.get('id') in ownership and peer.get('public_key') == ownership[peer['id']]
                and peer.get('iface_id') in account['interfaces']]

    def assigned_interface(account):
        selected = account.get('interfaces', [])
        if len(selected) != 1:
            raise StoreError('مدیر اصلی باید دقیقاً یک Interface برای این حساب تعیین کند.')
        row = next((item for item in interfaces() if item.get('id') == selected[0]), None)
        if row is None:
            raise StoreError('Interface تعیین‌شده در دسترس نیست؛ با مدیر اصلی تماس بگیرید.')
        return row

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
        if request.path == '/reseller' and not active_account():
            return redirect('/login')
        return send_from_directory(ui, 'index.html')

    @bp.get('/reseller/assets/<name>')
    def assets(name):
        if name not in {'reseller.js', 'reseller.css'}:
            abort(404)
        return send_from_directory(ui, name)

    @bp.get('/reseller/api/session')
    def status():
        factory = current_app.config.get('RESELLER_NATIVE_CSRF_FACTORY')
        if factory is None and native_scheme:
            from flask_wtf.csrf import generate_csrf
            factory = generate_csrf
        return jsonify(csrf=csrf(), account=active_account(), activation={
            'amount': 3000000, 'currency': 'toman', 'manual': True,
            'telegram_url': 'https://t.me/AshkanfathiOfficial',
            'telegram_username': 'AshkanfathiOfficial'}, native_csrf=factory() if factory else '')

    @bp.get('/reseller/api/credit')
    def credit():
        # A paused or pending account may inspect its own validity and capacity;
        # operational routes still require require_reseller's active check.
        identity = session.get('reseller_identity') or {}
        if (not isinstance(identity, dict) or type(identity.get('id')) is not int
                or type(identity.get('version')) is not int):
            return jsonify(error='login_required'), 401
        value = store.credit(identity['id'], auth_version=identity['version'])
        if value is None:
            return jsonify(error='login_required'), 401
        return jsonify(credit=value)

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
        # Paused accounts may sign in to see their own panel status. Protected
        # operations still require the effective account/ancestor access below.
        return jsonify(ok=True, account=account, paused=not account['active'], csrf=csrf())

    @bp.post('/reseller/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @bp.get('/resellers/api/accounts')
    @require_owner
    def accounts():
        rows = interfaces()
        return jsonify(accounts=account_details(store.all(), rows), interfaces=rows, audit=store.audit(),
                       operations=store.operations(), summary=store.summary(), csrf=csrf())

    @bp.get('/resellers/api/summary')
    @require_owner
    def summary():
        return jsonify(summary=store.summary())

    def child_creation_fee():
        return {'amount': 3000000, 'currency': 'toman', 'one_time': True,
                'payment_gateway': False,
                'message': 'هزینهٔ یک‌بارهٔ ساخت زیرنمایندگی ۳٬۰۰۰٬۰۰۰ تومان است؛ پرداخت آنلاین انجام نمی‌شود.'}

    @bp.get('/reseller/api/accounts')
    @require_reseller
    def child_accounts():
        assigned_interface(g.reseller)
        return jsonify(accounts=account_details(store.children(g.reseller['id'])),
                       creation_fee=child_creation_fee(), csrf=csrf())

    @bp.post('/reseller/api/accounts')
    @require_reseller
    def create_child_account():
        value = data()
        # Ownership, fees and activation are server decisions. Refuse attempts
        # to assign a parent, waive the fee, or grant immediate paid access.
        if set(value) - {'username', 'password', 'max_peers'}:
            raise StoreError('Interface زیرنماینده فقط توسط مدیر اصلی تعیین می‌شود.')
        assigned_interface(g.reseller)
        check_username(value.get('username'))
        capacity = value.get('max_peers', min(100, g.reseller['max_peers']))
        if type(capacity) is not int or not 1 <= capacity <= g.reseller['max_peers']:
            raise StoreError('سقف مشتری زیرنماینده باید در محدودهٔ حساب شما باشد.')
        account = store.create(str(value.get('username', '')), str(value.get('password', '')),
                               [], capacity, 'reseller:' + str(g.reseller['id']),
                               parent_id=g.reseller['id'], creation_fee_toman=3000000, allow_unassigned=True)
        return jsonify(account=account, signin_url='/login',
                       creation_fee=child_creation_fee()), 201

    @bp.post('/resellers/api/accounts')
    @require_owner
    def create_account():
        value = data()
        check_username(value.get('username'))
        selected = value.get('interfaces', [])
        if not isinstance(selected, list) or len(selected) != 1 or any(type(i) is not int for i in selected):
            raise StoreError('دقیقاً یک Interface برای نماینده تعیین کنید.')
        allowed = {row['id'] for row in interfaces()}
        if not set(selected) <= allowed:
            raise StoreError('Interface انتخاب‌شده وجود ندارد.')
        capacity = value.get('max_peers', 100)
        if type(capacity) is not int:
            raise StoreError('ظرفیت نامعتبر است.')
        account = store.create(str(value.get('username', '')), str(value.get('password', '')),
                               selected, capacity, g.reseller_owner, creation_fee_toman=3000000)
        return jsonify(account=account, signin_url='/login'), 201

    @bp.post('/resellers/api/accounts/<int:account_id>/approval')
    @require_owner
    def approve(account_id):
        value = data()
        if set(value) - {'approved', 'days'} or type(value.get('approved')) is not bool:
            raise StoreError('وضعیت تأیید نامعتبر است.')
        return jsonify(account=store.approve(account_id, g.reseller_owner,
            approved=value['approved'], days=value.get('days', 30)))

    @bp.get('/reseller/api/accounts/<int:account_id>/interface-port')
    @require_reseller
    def interface_port(account_id):
        from portal_provisioning import ProvisionError, provisioning_lock, suggest_port
        child_for_provision(account_id)
        try:
            with provisioning_lock(current_app.instance_path):
                return jsonify(port=suggest_port(port_snapshot(interfaces())))
        except ProvisionError as exc:
            return jsonify(error='port_scan_failed', message=str(exc)), 503

    @bp.post('/reseller/api/accounts/<int:account_id>/interface')
    @require_reseller
    def provision_child(account_id):
        from portal_provisioning import ProvisionError, child_network, provisioning_lock
        value = data()
        if set(value) != {'port'} or type(value.get('port')) is not int or not 20000 <= value['port'] <= 65535:
            raise StoreError('پورت باید بین ۲۰۰۰۰ و ۶۵۵۳۵ باشد.')
        parent_id = g.reseller['id']
        try:
            with provisioning_lock(current_app.instance_path):
                child_for_provision(account_id)
                rows = interfaces()
                if value['port'] in port_snapshot(rows):
                    return jsonify(error='listen_port_in_use', message='این پورت در حال استفاده است؛ پورت جدید پیشنهاد بگیرید.'), 409
                parent_iface = assigned_interface(g.reseller)
                name = 'wg_r' + str(account_id)
                if any(row.get('name') == name for row in rows):
                    raise StoreError('نام Interface موجود است؛ مدیر اصلی باید وضعیت را بررسی کند.')
                code, defaults = native().json('GET', '/api/iface/' + str(parent_iface['id']) + '/peer-defaults')
                if code != 200 or not isinstance(defaults, dict) or not isinstance(defaults.get('defaults'), dict):
                    raise CoreError('Parent interface defaults unavailable')
                policy = defaults['defaults']
                endpoint = str(policy.get('endpoint') or '').strip()
                endpoint_host = endpoint.rsplit(':', 1)[0].strip('[]') if ':' in endpoint else endpoint
                payload = {'child_id': account_id, 'name': name, 'address': child_network(rows),
                    'listen_port': value['port'], 'dns': policy.get('dns'), 'mtu': policy.get('mtu'),
                    'endpoint_host': endpoint_host, 'auto_up': True, 'auto_firewall': True,
                    'peer_defaults': {key: policy.get(key) for key in ('allowed_ips', 'persistent_keepalive', 'peer_endpoint')}}
                store.reserve_interface(parent_id, account_id, value['port'])
                try:
                    code, result = native().json('POST', '/api/portal/provision-child-interface', payload)
                    iface = result.get('interface') if isinstance(result, dict) else None
                    if code != 201 or not isinstance(iface, dict) or not iface.get('id') or result.get('up_error'):
                        store.fail_interface(parent_id, account_id, uncertain=True, detail='Native Interface startup was not confirmed')
                        return jsonify(error='interface_create_uncertain', message='ساخت یا راه‌اندازی تأیید نشد؛ مدیر وضعیت را بررسی کند.'), 502
                    account = store.attach_child_interface(parent_id, account_id, int(iface['id']))
                    return jsonify(account=account, interface={key: iface.get(key) for key in ('id', 'name', 'address', 'listen_port')},
                                   message='Interface ساخته شد؛ فعال‌سازی نماینده نیازمند تأیید مدیر اصلی است.'), 201
                except Exception:
                    store.fail_interface(parent_id, account_id, uncertain=True, detail='Native Interface outcome is uncertain')
                    return jsonify(error='interface_create_uncertain'), 502
        except ProvisionError as exc:
            return jsonify(error='port_scan_failed', message=str(exc)), 503

    @bp.post('/resellers/api/accounts/<int:account_id>/interface')
    @require_owner
    def assign_account_interface(account_id):
        value = data()
        if set(value) != {'iface_id'} or type(value.get('iface_id')) is not int:
            raise StoreError('شناسهٔ Interface نامعتبر است.')
        if value['iface_id'] not in {row['id'] for row in interfaces()}:
            raise StoreError('Interface انتخاب‌شده وجود ندارد.')
        account = store.assign_interface(account_id, value['iface_id'], g.reseller_owner)
        return jsonify(account=account, signin_required=True,
                       message='Interface تعیین شد؛ نماینده دوباره وارد شود. Peerهای قبلی حذف یا جابه‌جا نشدند.')

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
        iface = assigned_interface(g.reseller)
        fields = {'id','name','iface','status','panel_status','conn_status','used_bytes','data_limit','limit_unit','ttl_seconds'}
        return jsonify(peers=[{k: v for k, v in p.items() if k in fields} for p in owned_peers(g.reseller)],
                       assigned_interface={'id': iface['id'], 'name': iface['name']})

    @bp.post('/reseller/api/peers')
    @require_reseller
    def create_peer():
        value = data()
        if set(value) - {'name', 'days', 'volume_gb'}:
            raise StoreError('فقط نام، زمان و حجم مجاز است؛ تنظیمات فنی با مدیر اصلی است.')
        iface_id = assigned_interface(g.reseller)['id']
        name = str(value.get('name', '')).strip()
        days, volume = value.get('days', 30), value.get('volume_gb', 50)
        if not 1 <= len(name) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in name) or type(days) is not int or not 1 <= days <= 366 or type(volume) is not int or not 1 <= volume <= 100000:
            raise StoreError('نام مشتری، حجم یا زمان نامعتبر است.')
        request_id = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise StoreError('شناسهٔ یکتای درخواست الزامی است.')
        policy_code, policy = native().json('GET', '/api/iface/' + str(iface_id) + '/peer-defaults')
        if policy_code != 200 or not isinstance(policy, dict) or not isinstance(policy.get('defaults'), dict):
            raise CoreError('Administrator interface defaults are unavailable')
        technical = {key: policy['defaults'][key] for key in ('endpoint', 'allowed_ips', 'dns', 'persistent_keepalive', 'mtu', 'peer_endpoint') if key in policy['defaults']}
        operation = store.reserve(g.reseller['id'], request_id)
        if operation['state'] != 'new':
            if operation['state'] == 'complete':
                return jsonify(ok=True, id=operation['peer_id'], replay=True)
            return jsonify(error='operation_not_repeatable', message='وضعیت این درخواست نیازمند بررسی مدیر است.'), 409
        try:
            code, result = native().json('POST', '/api/peers', {**technical, 'iface_id': iface_id, 'scope': 'local', 'name': name,
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
        if action not in {'edit', 'renew'}:
            abort(404)
        peer = owned_peer(g.reseller, peer_id)
        value = data()
        if action == 'edit':
            if set(value) != {'name'} or not isinstance(value.get('name'), str):
                raise StoreError('فقط نام Peer قابل ویرایش است.')
            name = value['name'].strip()
            if not 1 <= len(name) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise StoreError('نام Peer نامعتبر است.')
            changes = {'action': 'rename', 'name': name}
        else:
            if set(value) != {'days', 'volume_gb'} or type(value.get('days')) is not int or not 1 <= value['days'] <= 366 or type(value.get('volume_gb')) is not int or not 1 <= value['volume_gb'] <= 100000:
                raise StoreError('مدت و حجم تمدید نامعتبر است.')
            request_id = request.headers.get('Idempotency-Key', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
                raise StoreError('شناسهٔ یکتای تمدید الزامی است.')
            changes = {'action': 'renew', 'days': value['days'], 'volume_gb': value['volume_gb'],
                       'request_id': 'r' + str(g.reseller['id']) + ':' + request_id}
        # Native id-based mutators have a TOCTOU boundary if another admin deletes
        # and recreates the same id. Use the atomic key-checked product endpoint.
        code, result = native().json('POST', '/api/portal/owned-peer-action',
                                    {'id': peer_id, 'public_key': store.ownership(g.reseller['id'])[peer_id], **changes})
        if code != 200:
            return jsonify(error='native_action_failed'), 409
        return jsonify(ok=True, replay=bool(result.get('replay')) if isinstance(result, dict) else False,
                       data_reset=False)

    @bp.get('/reseller/api/peers/<int:peer_id>/<kind>')
    @require_reseller
    def delivery(peer_id, kind):
        if kind not in {'config', 'qr', 'user-link'}:
            abort(404)
        owned_peer(g.reseller, peer_id)
        public_key = store.ownership(g.reseller['id'])[peer_id]
        if kind == 'user-link':
            code, value = native().json('GET', '/api/peer/' + quote(public_key, safe='') + '/shortlink')
            if code != 200 or not isinstance(value, dict) or not value.get('url'):
                return jsonify(error='delivery_unavailable'), 502
            return jsonify(url=value['url'])
        path = '/api/peer/' + quote(public_key, safe='') + ('/config_qr' if kind == 'qr' else '/config?download=1')
        code, body, content_type = native().call('GET', path)
        if code != 200:
            return jsonify(error='delivery_unavailable'), 502
        result = Response(body, content_type='image/png' if kind == 'qr' else 'text/plain; charset=utf-8')
        if kind == 'config':
            result.headers['Content-Disposition'] = f'attachment; filename="peer-{peer_id}.conf"'
        return result

    return bp
