"""Gateway regression checks using a real local HTTP upstream."""
import importlib.util
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

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
        os.environ["WG_PORTAL_SITE_ROOT"] = str(ROOT / "dist")
        os.environ["WG_PORTAL_PANEL_URL"] = "http://127.0.0.1:" + str(cls.server.server_port)
        spec = importlib.util.spec_from_file_location("gateway", ROOT / "deploy/linux/portal_proxy.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

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
            self.assertIn("Content-Security-Policy", response.headers)
        with self.client.get("/assets/not-present.css") as response:
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
