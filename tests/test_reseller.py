"""Scoped facade checks with a fake native API and temporary SQLite only."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/linux'))
from portal_reseller import reseller_blueprint, CoreError
from reseller_store import ResellerStore, StoreError


class Core:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.uncertain = False

    def json(self, method, path, payload=None, *, cookie=None):
        self.calls.append((method, path, payload, cookie))
        if path == '/api/admin':
            return (200, {'username': 'owner'}) if cookie == 'session=native-owner' else (401, {})
        if path == '/api/get-interfaces':
            return 200, {'interfaces': [{'id': 1, 'name': 'wg0'}, {'id': 2, 'name': 'wg1'}]}
        if path.endswith('/peer-defaults'):
            return 200, {'ok': True, 'defaults': {'endpoint':'vpn.example.test:51820', 'allowed_ips':'0.0.0.0/0, ::/0', 'dns':'1.1.1.1', 'mtu':1380, 'persistent_keepalive':25, 'peer_endpoint':''}}
        if path == '/api/peers' and method == 'GET':
            return 200, {'peers': self.rows}
        if path == '/api/peers' and method == 'POST':
            if self.uncertain:
                raise CoreError('timeout after submission')
            peer_id = len(self.rows) + 1
            peer = dict(id=peer_id, public_key=chr(64 + peer_id) * 43 + '=', iface_id=payload['iface_id'],
                        name=payload['name'], private_key='never-return-this', used_bytes=30)
            self.rows.append(peer)
            return 200, peer
        if path == '/api/portal/owned-peer-action':
            return 200, {'success': True}
        raise AssertionError(path)

    def call(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload, None))
        return 200, b'private-config', 'text/plain'


class ResellerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'resellers.sqlite3'
        self.store = ResellerStore(self.path)
        self.a = self.store.create('first', 'a-password-long-enough', [1], 2)
        self.b = self.store.create('second', 'another-long-password', [2], 2)
        self.store.update(self.a['id'], days=30)
        self.store.update(self.b['id'], days=30)
        self.core = Core()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='x' * 40, SESSION_COOKIE_NAME='wgportal_reseller_session', RESELLER_CORE=self.core)
        self.app.register_blueprint(reseller_blueprint('http://127.0.0.1:1', self.path, 'server-key'))
        self.client = self.app.test_client()
        self.csrf = self.client.get('/reseller/api/session').json['csrf']

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data, key='request-key-00000001', csrf=True):
        headers = {'Origin': 'https://localhost', 'Idempotency-Key': key}
        if csrf:
            headers['X-Reseller-CSRF'] = self.csrf
        return self.client.post(path, json=data, headers=headers)

    def login(self, username='first', password='a-password-long-enough'):
        result = self.post('/reseller/api/login', {'username': username, 'password': password})
        self.assertEqual(result.status_code, 200)
        self.csrf = result.json['csrf']

    def create(self, key='request-key-00000001'):
        return self.post('/reseller/api/peers', {'name': 'customer', 'days': 30, 'volume_gb': 50}, key)

    def test_distinct_cookie_and_no_admin_role_from_reseller(self):
        self.login()
        self.assertIsNone(self.client.get_cookie('session'))
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)
        self.assertEqual(self.post('/resellers/api/accounts', {'role': 'admin'}).status_code, 401)

    def test_owner_requires_native_cookie_not_ui_role(self):
        self.client.set_cookie('session', 'native-owner')
        result = self.client.get('/resellers/api/accounts')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json['accounts']), 2)
        self.assertNotIn('password_hash', result.get_data(as_text=True))

    def test_csrf_and_origin_required(self):
        self.assertEqual(self.post('/reseller/api/login', {}, csrf=False).status_code, 403)
        self.assertEqual(self.client.post('/reseller/api/login', json={}, headers={'Origin': 'https://evil.example', 'X-Reseller-CSRF': self.csrf}).status_code, 403)

    def test_body_limit(self):
        self.assertEqual(self.post('/reseller/api/login', {'username': 'x' * 33000}).status_code, 413)

    def test_scope_and_delivery_isolation(self):
        self.login()
        self.assertEqual(self.create().status_code, 201)
        self.core.rows.append({'id': 8, 'public_key': 'B' * 44, 'iface_id': 2, 'name': 'other-owner'})
        result = self.client.get('/reseller/api/peers')
        self.assertEqual(len(result.json['peers']), 1)
        self.assertNotIn('private_key', result.get_data(as_text=True))
        self.assertNotIn('public_key', result.get_data(as_text=True))
        self.assertEqual(self.client.get('/reseller/api/peers/8/config').status_code, 404)
        self.assertIn(self.post('/reseller/api/peers/8/edit', {'name':'forbidden'}).status_code, (403,404))
        self.assertEqual(self.client.get('/reseller/api/peers/1/config').data, b'private-config')

    def test_reused_native_id_does_not_grant_access(self):
        self.login()
        self.create()
        self.core.rows[0]['public_key'] = 'replacement'
        self.assertEqual(self.client.get('/reseller/api/peers').json['peers'], [])
        self.assertEqual(self.post('/reseller/api/peers/1/edit', {'name':'forbidden'}).status_code, 404)

    def test_action_calls_identity_guard(self):
        self.login()
        self.create()
        self.assertEqual(self.post('/reseller/api/peers/1/edit', {'name':'renamed'}).status_code, 200)
        self.assertEqual(self.core.calls[-1][1], '/api/portal/owned-peer-action')
        self.assertEqual(self.core.calls[-1][2]['public_key'], 'A' * 43 + '=')

    def test_create_scope_and_idempotency(self):
        self.login()
        self.assertIn(self.post('/reseller/api/peers', {'iface_id': 2}).status_code, (400,403))
        self.assertEqual(self.create().status_code, 201)
        self.assertTrue(self.create().json['replay'])
        self.assertEqual(len(self.core.rows), 1)
        self.assertEqual(self.create('request-key-00000002').status_code, 201)
        self.assertEqual(self.create('request-key-00000003').status_code, 400)

    def test_uncertain_create_not_retried(self):
        self.login()
        self.core.uncertain = True
        self.assertEqual(self.create().status_code, 502)
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(len(self.store.operations()), 1)

    def test_expiry_and_renewal_preserve_customer_data(self):
        self.login()
        self.create()
        with self.store.db(write=True) as db:
            db.execute('UPDATE resellers SET expires_at=1 WHERE id=?', (self.a['id'],))
        self.assertEqual(self.client.get('/reseller/api/peers').status_code, 403)
        self.assertEqual(self.client.get('/reseller/api/peers/1/config').status_code, 403)
        self.store.update(self.a['id'], days=30)
        self.assertEqual(len(self.client.get('/reseller/api/peers').json['peers']), 1)

    def test_renewal_receipt_prevents_extra_days(self):
        self.client.set_cookie('session', 'native-owner')
        path = '/resellers/api/accounts/' + str(self.a['id']) + '/renew'
        first = self.post(path, {'days': 30}).json['account']['expires_at']
        second = self.post(path, {'days': 30}).json['account']['expires_at']
        self.assertEqual(first, second)
        self.assertEqual(self.post(path, {'days': 31}).status_code, 400)

    def test_login_success_cannot_erase_other_users_failed_attempts(self):
        for _ in range(6):
            self.store.authenticate('first', 'incorrect', '192.0.2.4')
        self.assertIsNotNone(self.store.authenticate('second', 'another-long-password', '192.0.2.4')[0])
        for _ in range(4):
            self.store.authenticate('first', 'incorrect', '192.0.2.4')
        self.assertTrue(self.store.authenticate('first', 'a-password-long-enough', '192.0.2.4')[1])

    def test_newer_database_is_not_silently_downgraded(self):
        with self.store.db(write=True) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(StoreError):
            self.store.all()


if __name__ == '__main__':
    unittest.main()
