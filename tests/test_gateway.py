"""Gateway regression checks using a real local HTTP upstream."""
import importlib.util
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/login":
            self.send_response(302)
            self.send_header("Location", "/register")
            self.send_header("Set-Cookie", "session=one; HttpOnly")
            self.send_header("Set-Cookie", "csrf=two; SameSite=Lax")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Connection", "X-Internal")
        self.send_header("X-Internal", "must-not-forward")
        self.end_headers()
        self.wfile.write(self.path.encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body + b"|" + self.headers.get("X-CSRFToken", "").encode())


class GatewayChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        os.environ["WG_PORTAL_PANEL_URL"] = "http://127.0.0.1:" + str(cls.server.server_port)
        spec = importlib.util.spec_from_file_location("gateway", ROOT / "deploy/linux/portal_proxy.py")
        cls.module = importlib.util.module_from_spec(spec)
        cls.temporary = tempfile.TemporaryDirectory()
        sys.path.insert(0, str(ROOT / 'deploy/linux'))
        with patch.dict(os.environ, {'WG_PORTAL_RESELLER_DB': str(Path(cls.temporary.name) / 'resellers.sqlite3'), 'FLASK_SECRET_KEY': 'test-secret-' * 4}):
            spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def test_native_static_is_forwarded(self):
        with self.client.get("/static/panel.css") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"/static/panel.css")

    def test_login_redirect_and_both_cookies_survive(self):
        with self.client.get("/login") as response:
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/register")
            self.assertEqual(len(response.headers.getlist("Set-Cookie")), 2)

    def test_post_body_and_csrf_survive(self):
        with self.client.post("/users", data=b"name=test", headers={"X-CSRFToken": "token"}) as response:
            self.assertEqual(response.data, b"name=test|token")

    def test_query_string_survives(self):
        with self.client.get("/api/peers?name=a%26b&node=2") as response:
            self.assertEqual(response.data, b"/api/peers?name=a%26b&node=2")

    def test_connection_nominated_response_header_removed(self):
        with self.client.get("/users") as response:
            self.assertNotIn("X-Internal", response.headers)

    def test_frontend_and_missing_asset(self):
        with self.client.get("/") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'/')
        with self.client.get('/app.html') as response:
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers['Location'], '/')
        with self.client.get('/welcome') as response:
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers['Location'], '/')
        with self.client.get("/assets/not-present.css") as response:
            self.assertEqual(response.status_code, 404)

    def test_rejects_unconfigured_host(self):
        with self.client.get("/login", headers={"Host": "attacker.example"}) as response:
            self.assertEqual(response.status_code, 400)

    def test_reseller_assets_and_separate_session_are_reachable(self):
        for path in ('/reseller', '/resellers', '/reseller/assets/reseller.js', '/reseller/assets/reseller.css'):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)
        with self.client.get('/reseller/api/session') as response:
            self.assertIsNone(response.json['account'])
            cookie = response.headers.get('Set-Cookie', '')
            self.assertIn('wgportal_reseller_session=', cookie)
            self.assertIn('Secure', cookie)
            self.assertIn('HttpOnly', cookie)

    def test_configured_host_is_case_insensitive_and_port_aware(self):
        os.environ["WG_PORTAL_ALLOWED_HOSTS"] = "portal.example, 192.0.2.8"
        self.module.app.config["WG_PORTAL_ALLOWED_HOSTS"] = None
        try:
            with self.client.get("/", headers={"Host": "PORTAL.EXAMPLE:8765"}) as response:
                self.assertEqual(response.status_code, 200)
            with self.client.get("/", headers={"Host": "unknown.example"}) as response:
                self.assertEqual(response.status_code, 400)
        finally:
            os.environ.pop("WG_PORTAL_ALLOWED_HOSTS", None)
            self.module.app.config["WG_PORTAL_ALLOWED_HOSTS"] = None


if __name__ == "__main__":
    unittest.main()
