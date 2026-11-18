"""Render the real shared shell with temporary accounts and a fake native gate."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path(os.environ.get('SHELL_SUPPORT_ROOT', ROOT))
sys.path.insert(0, str(SUPPORT))
sys.path.insert(0, str(SUPPORT / 'deploy/linux'))
sys.path.insert(0, str(ROOT / 'deploy/linux'))
spec = importlib.util.spec_from_file_location('shell_routes_fixture', ROOT / 'deploy/linux/portal_reseller.py')
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)


class ResellerShellChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'resellers.sqlite3'
        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'), instance_path=self.temp.name)
        self.app.config.update(TESTING=True, SECRET_KEY='temporary-shell-fixture-secret',
                               SESSION_COOKIE_NAME='reseller-test-session')
        self.native_calls = []
        outer = self
        class Core:
            def json(self, method, path, payload=None, *, cookie=None):
                outer.native_calls.append((method, path, cookie))
                if path == '/api/admin':
                    return (200, {'username': '<native-owner>'}) if cookie == 'session=owner-cookie' else (401, {})
                raise AssertionError(path)
        self.app.register_blueprint(routes.reseller_blueprint('http://unused', self.db_path, 'server-key', core=Core()))
        self.app.jinja_env.globals['url_for'] = lambda endpoint, **kw: (
            '/static/' + kw['filename'] if endpoint == 'static' else '/' + endpoint.replace('.', '/'))
        self.app.context_processor(lambda: dict(
            current_user=SimpleNamespace(is_authenticated=False, is_admin=False, username='anonymous'),
            HAS_SETTINGS=True, HAS_NODES=True, csrf_token=lambda: 'native-fixture-csrf'))
        self.client = self.app.test_client()
        self.store = routes.ResellerStore(self.db_path)

    def login_reseller(self, *, pending=False):
        account = self.store.create('<reseller-name>', 'temporary-long-password', [1], 20)
        if not pending:
            self.store.approve(account['id'])
        csrf = self.client.get('/reseller/api/session').json['csrf']
        response = self.client.post('/reseller/api/login', json={'username': '<reseller-name>',
            'password': 'temporary-long-password'}, headers={'Origin':'https://localhost', 'X-Reseller-CSRF':csrf})
        self.assertEqual(response.status_code, 200, response.json)
        return account

    def test_owner_uses_native_shell_without_authenticating_native_user(self):
        self.client.set_cookie('session', 'owner-cookie')
        response = self.client.get('/resellers')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="panel-sidebar"', html)
        self.assertIn('class="main" id="main-content"', html)
        self.assertIn('data-portal-shell-role="owner"', html)
        self.assertIn('&lt;native-owner&gt;', html)
        self.assertIn('href="/users"', html)
        self.assertEqual(html.count('id="account-dialog"'), 1, 'No native profile/create dialog collision')
        self.assertNotIn('js/account-profile.js', html)
        self.assertIn('js/base.js', html)
        with self.client.session_transaction() as state:
            self.assertNotIn('_user_id', state)
            self.assertNotIn('reseller_identity', state)

    def test_reseller_uses_same_shell_but_has_no_global_admin_controls(self):
        self.login_reseller()
        response = self.client.get('/reseller')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="panel-sidebar"', html)
        self.assertIn('class="main" id="main-content"', html)
        self.assertIn('data-portal-shell-role="reseller"', html)
        self.assertIn('&lt;reseller-name&gt;', html)
        for forbidden in ('id="panel-update-modal"', 'id="nx-command"', 'js/panel-update.js',
                          'js/panel-version.js', 'js/account-profile.js', 'href="/users"', 'href="/nodes"', '/api_timezone'):
            self.assertNotIn(forbidden, html, forbidden)
        self.assertIn('js/base.js', html)
        self.assertIn('reseller-nav-services', html)
        self.assertEqual(self.client.get('/resellers').status_code, 302)
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)
        with self.client.session_transaction() as state:
            self.assertNotIn('_user_id', state)

    def test_pending_reseller_can_render_payment_and_security_workspace(self):
        self.login_reseller(pending=True)
        response = self.client.get('/reseller')
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="reseller-security"', response.get_data(as_text=True))
        self.assertIn('payment-pending-panel', response.get_data(as_text=True))

    def test_anonymous_both_workspace_paths_redirect(self):
        for path in ('/reseller', '/resellers'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers['Location'].startswith('/login'))

    def test_inline_bootstrap_has_per_response_nonce_and_strict_csp(self):
        self.client.set_cookie('session', 'owner-cookie')
        responses = [self.client.get('/resellers') for _ in range(2)]
        nonces = []
        for response in responses:
            html = response.get_data(as_text=True)
            csp = response.headers['Content-Security-Policy']
            nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
            nonces.append(nonce)
            self.assertNotIn("'unsafe-inline'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            for attributes in re.findall(r'<script([^>]*)>', html):
                if 'src=' not in attributes:
                    self.assertIn('nonce="' + nonce + '"', attributes)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotEqual(*nonces)


if __name__ == '__main__':
    unittest.main()
