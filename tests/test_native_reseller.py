"""Same-process reseller integration using temporary SQLite and fake native routes."""
import os
import base64
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask, g, jsonify, request, session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portal_reseller_native import install_reseller, NativeApplicationCore, ResellerSessions
from deploy.linux.portal_reseller import CoreError
from deploy.linux.reseller_store import ResellerStore


class NativeResellerChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / 'resellers.sqlite3'
        self.app = Flask(__name__, root_path=str(ROOT), instance_path=self.temporary.name)
        self.app.config.update(TESTING=True, SECRET_KEY='native-secret-' * 4, API_KEY='native-key-' * 4)
        self.rows = []
        self.native_calls = []
        self.interface_rows = [{'id':1,'name':'wg0','address':'10.66.66.1/24','listen_port':51820}, {'id':2,'name':'wg1','address':'10.67.67.1/24','listen_port':51821}]
        self.native_interface_calls = []
        self.native_interface_error = None
        from portal_provisioning import occupied_ports as actual_occupied
        scanner = patch('portal_provisioning.occupied_ports', side_effect=lambda rows, path, reservations=(), **kwargs: actual_occupied(rows, path, reservations, scanner=lambda:set(), **kwargs))
        scanner.start()
        self.addCleanup(scanner.stop)

        @self.app.before_request
        def native_context():
            if request.path.startswith('/api/'):
                if getattr(g, 'reseller_marker', False):
                    raise AssertionError('Inner native call reused reseller application context')
                self.native_calls.append((request.path, request.headers.get('Cookie', ''),
                                          request.headers.get('X-API-KEY', '')))
            else:
                g.reseller_marker = True

        @self.app.get('/fixture-owner')
        def fixture_owner():
            session['_user_id'] = 'owner'
            return 'signed in'

        @self.app.get('/api/admin')
        def admin():
            if session.get('_user_id') == 'owner' or request.cookies.get('remember_token') == 'valid-native-remember':
                return jsonify(username='mother')
            return jsonify(error='login_required'), 401

        @self.app.get('/api/get-interfaces')
        def interfaces():
            if request.headers.get('X-API-KEY') != self.app.config['API_KEY']:
                return jsonify(error='denied'), 401
            return jsonify(interfaces=self.interface_rows)

        @self.app.post('/api/portal/provision-child-interface')
        def fake_provision():
            if request.headers.get('X-API-KEY') != self.app.config['API_KEY']:
                return jsonify(error='denied'), 401
            value=request.get_json()
            self.native_interface_calls.append(value)
            if self.native_interface_error:
                code,body=self.native_interface_error
                return jsonify(body),code
            iface={key:value[key] for key in ('name','address','listen_port')}
            iface['id']=max(row['id'] for row in self.interface_rows)+1
            self.interface_rows.append(iface)
            return jsonify(interface=iface),201

        @self.app.route('/api/peers', methods=['GET', 'POST'])
        def peers():
            if request.headers.get('X-API-KEY') != self.app.config['API_KEY']:
                return jsonify(error='denied'), 401
            if request.method == 'GET':
                return jsonify(peers=self.rows)
            payload = request.get_json()
            peer = dict(id=len(self.rows) + 1, iface_id=payload['iface_id'],
                        public_key=base64.b64encode(hashlib.sha256(str(len(self.rows)+1).encode()).digest()).decode(), name=payload['name'], private_key='never-deliver')
            self.rows.append(peer)
            return jsonify(peer)

        @self.app.get('/api/peer/<path:key>/config')
        def config(key):
            return 'native-config', 200, {'Content-Type': 'text/plain'}

        @self.app.get('/api/peer/<path:key>/config_qr')
        def config_qr(key):
            return b'\x89PNG\r\n\x1a\nfixture', 200, {'Content-Type':'image/png'}

        @self.app.get('/api/peer/<path:key>/shortlink')
        def shortlink(key):
            if request.headers.get('X-API-KEY') != self.app.config['API_KEY']:
                return jsonify(error='denied'), 401
            return jsonify(url='http://localhost/u/opaque-user-token')

        @self.app.get('/api/iface/<int:iid>/peer-defaults')
        def peer_defaults(iid):
            return jsonify(ok=True, defaults={'endpoint':'vpn.example.test:51820', 'allowed_ips':'0.0.0.0/0, ::/0', 'dns':'1.1.1.1', 'mtu':1380, 'persistent_keepalive':25, 'peer_endpoint':''})

        self.csrf_guard = Mock()
        with patch.dict(os.environ, {'WG_PANEL_RESELLER_DB': str(self.database)}):
            install_reseller(self.app, self.csrf_guard, native_admin_lookup=lambda username: username.casefold() == "mother")
        self.app.config['RESELLER_NATIVE_CSRF_FACTORY'] = lambda: 'native-fixture-csrf'
        self.client = self.app.test_client()
        self.csrf = self.client.get('/reseller/api/session').json['csrf']
        self.store = ResellerStore(self.database)
        from portal_provision_defaults import ProvisionDefaultsStore, DEFAULTS
        ProvisionDefaultsStore(self.app.instance_path).save(dict(DEFAULTS,endpoint_host='vpn.example.test',network_pool='10.88.0.0/16',child_max_peers=2))

    def post(self, path, payload, *, client=None, csrf=None):
        return (client or self.client).post(path, json=payload, headers={
            'Origin': 'http://localhost', 'X-Reseller-CSRF': csrf or self.csrf,
            'Idempotency-Key': 'temporary-request-id-0001'})

    def account(self, username='dealer'):
        value = self.store.create(username, 'a-safe-test-password', [1], 3)
        self.store.approve(value['id'], days=30)
        return value

    def login(self, client=None, csrf=None):
        result = self.post('/reseller/api/login', {'username': 'dealer', 'password': 'a-safe-test-password'},
                           client=client, csrf=csrf)
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        if client is None:
            self.csrf = result.json['csrf']
        return result

    def test_owner_management_is_native_and_works_on_http(self):
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)
        self.client.get('/fixture-owner')
        response = self.post('/resellers/api/accounts', dict(username='new-dealer', password='a-safe-test-password', max_peers=3))
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        account_id = response.json['account']['id']
        self.assertEqual(response.json['interface']['listen_port'],51825)
        self.assertEqual(response.json['account']['interfaces'],[response.json['interface']['id']])
        self.assertFalse(self.store.get(account_id)['active'])
        response = self.post(f'/resellers/api/accounts/{account_id}/approval', {'approved': True, 'days': 30})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(self.store.get(account_id)['active'])
        calls = [item for item in self.native_calls if item[0] == '/api/admin']
        self.assertTrue(calls)
        self.assertTrue(all(not key for _, _, key in calls))

    def test_reseller_login_logout_preserve_native_owner_session(self):
        self.account()
        self.client.get('/fixture-owner')
        original = self.client.get_cookie('session').value
        self.login()
        self.assertEqual(self.client.get_cookie('session').value, original)
        self.assertEqual(self.client.get('/api/admin').status_code, 200)
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 200)
        self.assertEqual(self.post('/reseller/api/logout', {}).status_code, 200)
        self.assertEqual(self.client.get_cookie('session').value, original)
        self.assertEqual(self.client.get('/api/admin').status_code, 200)

    def test_reseller_cookie_never_authenticates_native_admin(self):
        self.account()
        self.login()
        self.assertIsNone(self.client.get_cookie('session'))
        self.assertEqual(self.client.get('/api/admin').status_code, 401)
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)
        self.client.set_cookie('session', self.client.get_cookie(ResellerSessions.cookie_name).value)
        self.assertEqual(self.client.get('/api/admin').status_code, 401)

    def test_native_cookie_cannot_be_transplanted_into_reseller_cookie(self):
        self.account()
        serializer = self.app.session_interface.native.get_signing_serializer(self.app)
        token = serializer.dumps({'reseller_identity': {'id': 1, 'version': 1}, '_user_id': 'owner'})
        self.client.set_cookie(ResellerSessions.cookie_name, token)
        self.assertIsNone(self.client.get('/reseller/api/session').json['account'])
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)

    def test_remembered_owner_is_verified_only_by_native_route(self):
        self.client.set_cookie('remember_token', 'valid-native-remember')
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 200)
        self.assertIsNone(self.client.get_cookie('session'))
        self.client.set_cookie('remember_token', 'forged')
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)

    def test_csrf_and_origin_remain_required_on_http(self):
        self.account()
        payload = {'username': 'dealer', 'password': 'a-safe-test-password'}
        self.assertEqual(self.client.post('/reseller/api/login', json=payload, headers={'Origin': 'http://localhost'}).status_code, 403)
        for origin in ('http://evil.example', 'https://localhost', 'null'):
            response = self.client.post('/reseller/api/login', json=payload,
                headers={'Origin': origin, 'X-Reseller-CSRF': self.csrf})
            self.assertEqual(response.status_code, 403)
        self.login()

    def test_peer_create_uses_existing_native_routes_without_network(self):
        self.account()
        self.login()
        with patch('requests.Session', side_effect=AssertionError('No HTTP self-call allowed')):
            response = self.post('/reseller/api/peers', {'name': 'customer', 'days': 30, 'volume_gb': 10})
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
            peers = self.client.get('/reseller/api/peers')
            self.assertEqual(peers.status_code, 200)
        self.assertEqual(len(self.rows), 1)
        self.assertNotIn('private_key', peers.get_data(as_text=True))
        calls = [item for item in self.native_calls if item[0] == '/api/peers']
        self.assertTrue(all(key == self.app.config['API_KEY'] and not cookie for _, cookie, key in calls))
        self.assertIsNone(self.client.get_cookie('session'))

    def test_isolated_cookie_secure_flag_tracks_current_native_transport(self):
        result = self.app.test_client().get('/reseller/api/session', base_url='https://localhost')
        cookie = next(value for value in result.headers.getlist('Set-Cookie') if value.startswith(ResellerSessions.cookie_name + '='))
        self.assertIn('; Secure', cookie)
        self.assertIn('; HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        result = self.app.test_client().get('/reseller/api/session', base_url='http://localhost')
        cookie = next(value for value in result.headers.getlist('Set-Cookie') if value.startswith(ResellerSessions.cookie_name + '='))
        self.assertNotIn('; Secure', cookie)

    def test_native_adapter_rejects_unapproved_route(self):
        with self.app.test_request_context('/'):
            with self.assertRaises(CoreError):
                NativeApplicationCore(self.app).call('POST', '/api/panel/update', {})

    def test_existing_legacy_reseller_database_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / 'deploy/linux/runtime/resellers.sqlite3'
            preserved = ResellerStore(legacy)
            account = preserved.create('preserved-dealer', 'a-safe-test-password', [1], 2)
            app = Flask('legacy-fixture', root_path=str(root), instance_path=str(root / 'instance'))
            app.config.update(TESTING=True, SECRET_KEY='legacy-secret-' * 4, RESELLER_NATIVE_CSRF_FACTORY=lambda:'native-fixture-csrf')
            with patch.dict(os.environ, {'WG_PANEL_RESELLER_DB': ''}):
                install_reseller(app, Mock())
            app.test_client().get('/reseller/api/session')
            self.assertEqual(preserved.get(account['id'])['username'], 'preserved-dealer')
            self.assertFalse((root / 'instance/resellers.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
