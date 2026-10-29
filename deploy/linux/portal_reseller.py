"""Scoped reseller extension around native WireGuard operations.

Reseller sessions never become WG_Panel administrator sessions. Only explicit
operations below use the server-side native API credential.
"""
from functools import wraps
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
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

    def provision_defaults():
        from portal_provision_defaults import ProvisionDefaultsStore
        return ProvisionDefaultsStore(current_app.instance_path)

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
        if not account or account.get('deleted') or account['auth_version'] != value.get('version'):
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

    def prepare_child_interface(rows):
        from portal_provisioning import suggest_port, child_network
        policy = provision_defaults().required()
        port = suggest_port(port_snapshot(rows))
        address = child_network(rows, policy['network_pool'], policy['subnet_prefix'])
        return policy, port, address

    def provision_account_interface(child, policy, port, address):
        """Caller holds the shared admission lock from allocation to attachment."""
        parent_id, child_id = child['parent_id'], child['id']
        payload = {'child_id': child_id, 'name': 'wg_r' + str(child_id), 'address': address,
            'listen_port': port, 'dns': policy['dns'], 'mtu': policy['mtu'],
            'endpoint_host': policy['endpoint_host'], 'auto_up': True,
            'auto_firewall': policy['auto_firewall'],
            'peer_defaults': {key: policy[key] for key in ('allowed_ips', 'persistent_keepalive')}}
        store.reserve_interface(parent_id, child_id, port)
        try:
            code, result = native().json('POST', '/api/portal/provision-child-interface', payload)
            iface = result.get('interface') if isinstance(result, dict) else None
            if (code != 201 or not isinstance(iface, dict) or type(iface.get('id')) is not int
                    or iface['id'] < 1 or result.get('up_error')):
                raise CoreError('Native Interface startup was not confirmed')
            account = store.attach_child_interface(parent_id, child_id, iface['id'])
        except Exception:
            # Native creation is not a transaction with the additive store.
            # Retain both the account and reservation for mother-admin recovery.
            store.fail_interface(parent_id, child_id, uncertain=True,
                                 detail='Native Interface outcome was not confirmed')
            return jsonify(error='interface_create_uncertain', account=store.get(child_id),
                message='حساب ثبت شد، اما ساخت یا راه‌اندازی Interface تأیید نشد؛ دوباره نسازید و با مدیر تماس بگیرید.'), 502
        safe = {'id': iface['id'], 'name': payload['name'], 'address': address, 'listen_port': port}
        account['assigned_interfaces'] = [safe]
        return jsonify(account=account, interface=safe, signin_url='/login', creation_fee=child_creation_fee(),
            message='زیرنماینده و Interface اختصاصی ساخته شدند؛ فعال‌سازی پس از تأیید مدیر اصلی است.'), 201

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

    from portal_provision_defaults import ProvisionDefaultsError, ProvisionDefaultsMissing
    from portal_provisioning import ProvisionError, PortPoolExhausted, NetworkPoolExhausted

    @bp.errorhandler(ProvisionDefaultsMissing)
    def defaults_missing(error):
        return jsonify(error='provision_defaults_required', message=str(error)), 409

    @bp.errorhandler(ProvisionDefaultsError)
    def defaults_failure(error):
        return jsonify(error='provision_defaults_invalid', message=str(error)), 400 if request.method == 'PUT' else 503

    @bp.errorhandler(PortPoolExhausted)
    def ports_full(error):
        return jsonify(error='port_pool_exhausted', message=str(error)), 409

    @bp.errorhandler(NetworkPoolExhausted)
    def networks_full(error):
        return jsonify(error='network_pool_exhausted', message=str(error)), 409

    @bp.errorhandler(ProvisionError)
    def allocation_failure(error):
        return jsonify(error='interface_admission_failed', message=str(error)), 503

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

    @bp.get('/resellers/api/peer-owners')
    @require_owner
    def peer_owners():
        try:
            return jsonify(owners=store.peer_owners())
        except (OSError, sqlite3.Error):
            # An unavailable index must never look like a successful empty
            # index: that would mislabel reseller Peers as native-admin Peers.
            return jsonify(error='owner_mapping_unavailable'), 503

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

    @bp.route('/resellers/api/interface-defaults', methods=['GET', 'PUT'])
    @require_owner
    def interface_defaults():
        if request.method == 'GET':
            return jsonify(**provision_defaults().load())
        return jsonify(**provision_defaults().save(data()))

    @bp.post('/reseller/api/accounts')
    @require_reseller
    def create_child_account():
        value = data()
        if set(value) != {'username', 'password'} or any(not isinstance(value[key], str) for key in value):
            raise StoreError('برای زیرنماینده فقط نام کاربری و رمز را وارد کنید؛ Interface و ظرفیت خودکار هستند.')
        check_username(value['username'])
        # Admission and defaults must succeed before creating a billed account.
        from portal_provisioning import provisioning_lock
        with provisioning_lock(current_app.instance_path):
            assigned_interface(g.reseller)
            rows = interfaces()
            policy, port, address = prepare_child_interface(rows)
            capacity = min(policy['child_max_peers'], g.reseller['max_peers'])
            account = store.create(value['username'], value['password'], [], capacity,
                'reseller:' + str(g.reseller['id']), parent_id=g.reseller['id'],
                creation_fee_toman=3000000, allow_unassigned=True)
            return provision_account_interface(account, policy, port, address)

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
        if type(value.get('days', 30)) is not int or not 1 <= value.get('days', 30) <= 366:
            raise StoreError('مدت تأیید باید بین ۱ تا ۳۶۶ روز باشد.')
        if value['approved']:
            from portal_provisioning import provisioning_lock
            with provisioning_lock(current_app.instance_path):
                account = store.get(account_id)
                if not account or account.get('deleted'):
                    abort(404)
                job = account.get('interface_provision')
                if job and job['state'] in {'pending', 'uncertain'}:
                    return jsonify(error='interface_review_required', account=account,
                        message='نتیجهٔ ساخت Interface نامشخص است؛ مدیر باید Interface واقعی را بررسی و با گزینهٔ تعیین Interface ثبت کند.'), 409
                if not account['interfaces']:
                    if account.get('parent_id') is None or job is not None:
                        return jsonify(error='interface_required', account=account,
                            message='قبل از تأیید، مدیر باید یک Interface واقعی برای این حساب تعیین کند.'), 409
                    # Older child forms saved a billed account before an
                    # Interface existed. Recover each such row independently.
                    policy, port, address = prepare_child_interface(interfaces())
                    provisioned = provision_account_interface(account, policy, port, address)
                    if provisioned[1] != 201:
                        return provisioned
                    account = store.get(account_id)
                assigned_interface(account)
                return jsonify(account=store.approve(account_id, g.reseller_owner,
                    approved=True, days=value.get('days', 30)))
        return jsonify(account=store.approve(account_id, g.reseller_owner,
            approved=value['approved'], days=value.get('days', 30)))

    def archive_account(account_id, parent_id=None):
        confirmation = data()
        if set(confirmation) != {'confirm'} or confirmation.get('confirm') is not True:
            raise StoreError('حذف نماینده نیازمند تأیید صریح است.')
        account = store.get(account_id)
        if account is None or (parent_id is not None and account.get('parent_id') != parent_id):
            abort(404)
        actor = g.reseller_owner if parent_id is None else 'reseller:' + str(parent_id)
        account = store.archive(account_id, actor, parent_id=parent_id)
        manager = current_app.extensions.get('reseller_bots')
        if manager is not None:
            manager.store.disable(account_id)
        return jsonify(ok=True, account=account, resources_preserved=True,
                       message='دسترسی نماینده حذف شد؛ Peerها و زیرنماینده‌ها حفظ شدند و زیرمجموعه تا بررسی مدیر دسترسی عملیاتی ندارد.')

    @bp.delete('/resellers/api/accounts/<int:account_id>')
    @require_owner
    def delete_account(account_id):
        return archive_account(account_id)

    @bp.delete('/reseller/api/accounts/<int:account_id>')
    @require_reseller
    def delete_child_account(account_id):
        return archive_account(account_id, g.reseller['id'])

    @bp.get('/reseller/api/accounts/<int:account_id>/interface-port')
    @require_reseller
    def interface_port(account_id):
        from portal_provisioning import provisioning_lock
        child_for_provision(account_id)
        with provisioning_lock(current_app.instance_path):
            _policy, port, _address = prepare_child_interface(interfaces())
            return jsonify(port=port)

    @bp.post('/reseller/api/accounts/<int:account_id>/interface')
    @require_reseller
    def provision_child(account_id):
        # Compatibility recovery for an older unassigned child with no prior
        # attempt. The client can never choose a port or override admin policy.
        if data():
            raise StoreError('Interface و پورت خودکار هستند؛ تنظیم دستی توسط نماینده مجاز نیست.')
        from portal_provisioning import provisioning_lock
        with provisioning_lock(current_app.instance_path):
            child = child_for_provision(account_id)
            assigned_interface(g.reseller)
            policy, port, address = prepare_child_interface(interfaces())
            return provision_account_interface(child, policy, port, address)

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
        name = str(value.get('name', '')).strip()
        days, volume = value.get('days', 30), value.get('volume_gb', 50)
        if not 1 <= len(name) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in name) or type(days) is not int or not 1 <= days <= 366 or type(volume) is not int or not 1 <= volume <= 100000:
            raise StoreError('نام مشتری، حجم یا زمان نامعتبر است.')
        request_id = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise StoreError('شناسهٔ یکتای درخواست الزامی است.')
        result, status = create_owned_peer(name, days, volume, request_id)
        return jsonify(**result), status

    def peer_technical_defaults(iface_id):
        policy_code, policy = native().json('GET', '/api/iface/' + str(iface_id) + '/peer-defaults')
        if policy_code != 200 or not isinstance(policy, dict) or not isinstance(policy.get('defaults'), dict):
            raise CoreError('Administrator interface defaults are unavailable')
        return {key: policy['defaults'][key] for key in ('endpoint', 'allowed_ips', 'dns', 'persistent_keepalive', 'mtu', 'peer_endpoint') if key in policy['defaults']}

    def create_owned_peer(name, days, volume, request_id, *, technical=None, expected_iface=None):
        """One bounded native create path for single and bulk operations."""
        iface_id = assigned_interface(g.reseller)['id']
        if expected_iface is not None and iface_id != expected_iface:
            raise StoreError('Interface نماینده تغییر کرده است؛ ساخت انبوه متوقف شد.')
        technical = peer_technical_defaults(iface_id) if technical is None else technical
        operation = store.reserve(g.reseller['id'], request_id,
                                  {'iface_id': iface_id, 'name': name, 'days': days, 'volume_gb': volume})
        if operation['state'] != 'new':
            if operation['state'] == 'complete':
                return {'ok': True, 'id': operation['peer_id'], 'replay': True}, 200
            return {'error': 'operation_not_repeatable', 'uncertain': operation['state'] in {'pending', 'uncertain'},
                    'message': 'وضعیت این درخواست نیازمند بررسی مدیر است.'}, 409
        try:
            code, result = native().json('POST', '/api/peers', {**technical, 'iface_id': iface_id, 'scope': 'local', 'name': name,
                'time_limit_days': days, 'data_limit_value': volume, 'data_limit_unit': 'Gi', 'unlimited': False})
            if code != 200 or not isinstance(result, dict) or not result.get('id') or not result.get('public_key'):
                store.failed(operation['id'], uncertain=True)
                return {'error': 'native_create_failed', 'uncertain': True, 'message': 'ساخت تأیید نشد؛ پیش از تلاش جدید مدیر بررسی کند.'}, 502
            store.finish(operation['id'], g.reseller['id'], int(result['id']), str(result['public_key']))
            return {'ok': True, 'id': result['id']}, 201
        except Exception:
            store.failed(operation['id'], uncertain=True)
            return {'error': 'create_uncertain', 'uncertain': True, 'message': 'نتیجهٔ ساخت نامشخص است؛ این درخواست را تکرار نکنید و به مدیر اطلاع دهید.'}, 502

    @bp.post('/reseller/api/peers/bulk')
    @require_reseller
    def create_peer_bulk():
        value = data()
        if set(value) != {'name_prefix', 'count', 'days', 'volume_gb'}:
            raise StoreError('فقط پیشوند نام، تعداد، روز و حجم برای ساخت انبوه مجاز است.')
        prefix, count, days, volume = (value.get(key) for key in ('name_prefix', 'count', 'days', 'volume_gb'))
        if (not isinstance(prefix, str) or not prefix.strip() or type(count) is not int or not 1 <= count <= 20
                or type(days) is not int or not 1 <= days <= 366
                or type(volume) is not int or not 1 <= volume <= 100000):
            raise StoreError('اطلاعات ساخت انبوه نامعتبر است؛ تعداد باید بین ۱ تا ۲۰ باشد.')
        prefix = prefix.strip()
        if len(prefix + '-' + str(count)) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in prefix):
            raise StoreError('پیشوند نام بیش از حد بلند یا نامعتبر است.')
        request_id = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise StoreError('شناسهٔ یکتای ساخت انبوه الزامی است.')
        iface_id = assigned_interface(g.reseller)['id']
        technical = peer_technical_defaults(iface_id)
        snapshot = {'name_prefix': prefix, 'count': count, 'days': days, 'volume_gb': volume,
                    'iface_id': iface_id}
        previous = store.begin_batch(g.reseller['id'], request_id, snapshot)
        if previous is not None:
            return jsonify(**previous), 200 if previous['complete'] else 409
        result = {'ok': False, 'requested': count, 'created': [], 'failed': [], 'uncertain': True, 'complete': False}
        for index in range(1, count + 1):
            name = prefix + '-' + str(index)
            item_key = 'b' + hashlib.sha256((request_id + ':' + str(index)).encode()).hexdigest()
            try:
                outcome, status = create_owned_peer(name, days, volume, item_key,
                    technical=technical, expected_iface=iface_id)
            except (StoreError, CoreError) as exc:
                # No native create has begun: admission/defaults failed.
                outcome, status = {'error': str(exc)}, 409
            if status in {200, 201} and outcome.get('ok'):
                result['created'].append({'id': outcome['id'], 'name': name, 'replay': bool(outcome.get('replay'))})
            else:
                result['failed'].append({'index': index, 'error': outcome.get('error', 'create_failed')})
            # Persist after each item. A process crash retains an uncertain
            # batch and cannot replay already issued WireGuard operations.
            store.batch_progress(g.reseller['id'], request_id, result)
            if outcome.get('uncertain'):
                return jsonify(**result), 409
        result.update(ok=len(result['created']) == count, uncertain=False, complete=True)
        store.batch_progress(g.reseller['id'], request_id, result)
        return jsonify(**result), 201 if result['ok'] else 207

    @bp.delete('/reseller/api/peers/<int:peer_id>')
    @require_reseller
    def delete_peer(peer_id):
        confirmation = data()
        if set(confirmation) != {'confirm'} or confirmation.get('confirm') is not True:
            raise StoreError('حذف Peer نیازمند تأیید صریح است.')
        public_key = store.ownership(g.reseller['id']).get(peer_id)
        if public_key is None:
            if store.peer_was_deleted(g.reseller['id'], peer_id):
                return jsonify(ok=True, replay=True)
            abort(404)
        # Native guard performs the final key comparison under its write lock;
        # no ordinary id-only native deletion route is exposed to resellers.
        code, result = native().json('POST', '/api/portal/owned-peer-action',
                                    {'id': peer_id, 'public_key': public_key, 'action': 'delete',
                                     'iface_id': assigned_interface(g.reseller)['id']})
        if code != 200 or not isinstance(result, dict) or not result.get('success'):
            return jsonify(error='native_delete_failed', message='حذف تأیید نشد؛ ظرفیت و مالکیت حفظ شدند.'), 409
        store.forget_peer(g.reseller['id'], peer_id, public_key)
        return jsonify(ok=True, replay=bool(result.get('absent')))

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
