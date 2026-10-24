import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PortalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ["WG_PORTAL_PUBLIC_IP"] = "8.8.8.8"
        os.environ["WG_PORTAL_ALLOWED_HOSTS"] = "8.8.8.8,vpn.example.test"
        os.environ["WG_PORTAL_ACME_WEBROOT"] = cls.temp.name
        spec = importlib.util.spec_from_file_location("portal_http", ROOT / "deploy/linux/portal_http.py")
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()
        cls.module.PENDING_HOST = Path(cls.temp.name) / "pending-host"

    @classmethod
    def tearDownClass(cls):
        for key in ("WG_PORTAL_PUBLIC_IP", "WG_PORTAL_ALLOWED_HOSTS", "WG_PORTAL_ACME_WEBROOT"):
            os.environ.pop(key, None)
        cls.temp.cleanup()

    def test_non_challenge_http_redirects_to_configured_ip_not_host_header(self):
        response = self.client.get("/login?next=secret", headers={"Host": "vpn.example.test"})
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["Location"], "https://8.8.8.8:8767/setup")

    def test_unrecognized_host_is_rejected(self):
        response = self.client.get("/", headers={"Host": "attacker.test"})
        self.assertEqual(response.status_code, 400)

    def test_only_acme_challenge_files_are_served(self):
        challenge_dir = Path(self.temp.name, ".well-known", "acme-challenge")
        challenge_dir.mkdir(parents=True, exist_ok=True)
        (challenge_dir / "token-1").write_text("challenge-response", encoding="ascii")
        response = self.client.get("/.well-known/acme-challenge/token-1", headers={"Host": "8.8.8.8"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "challenge-response")
        response.close()
        self.assertEqual(self.client.get("/assets/setup.js", headers={"Host": "8.8.8.8"}).status_code, 308)

    def test_http_welcome_contains_only_secure_link_and_no_secret_form(self):
        response = self.client.get("/", headers={"Host": "8.8.8.8"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('https://8.8.8.8:8767/', response.text)
        self.assertIn("self-signed", response.text)
        self.assertNotIn("<form", response.text)
        self.assertNotIn("<input", response.text)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.client.post("/", headers={"Host": "8.8.8.8"}, json={"token": "ignored"}).status_code, 405)

    def test_pending_domain_is_allowed_only_for_challenge_and_expires(self):
        self.module.PENDING_HOST.write_text(json.dumps({"host": "new.example.test", "expires_at": time.time() + 60}))
        try:
            self.assertEqual(self.client.get("/", headers={"Host": "new.example.test"}).status_code, 400)
            response = self.client.get("/.well-known/acme-challenge/missing", headers={"Host": "new.example.test"})
            self.assertEqual(response.status_code, 404)
            self.module.PENDING_HOST.write_text(json.dumps({"host": "new.example.test", "expires_at": time.time() - 1}))
            self.assertEqual(self.client.get("/.well-known/acme-challenge/missing", headers={"Host": "new.example.test"}).status_code, 400)
        finally:
            self.module.PENDING_HOST.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
