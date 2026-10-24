import importlib.util
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "deploy/linux"
sys.path.insert(0, str(LINUX))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-that-is-long-and-only-for-tests")
os.environ.setdefault("WG_PORTAL_PUBLIC_IP", "8.8.8.8")
os.environ.setdefault("SETUP_TOKEN", "K" * 48)

SPEC = importlib.util.spec_from_file_location("portal_setup", LINUX / "portal_setup.py")
portal = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = portal
SPEC.loader.exec_module(portal)


class PortalSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, value in {"RUNTIME": self.root, "PORTAL_ENV": self.root / "portal.env",
                            "TLS_CURRENT": self.root / "tls/current", "PENDING_HOST": self.root / "acme-pending-host"}.items():
            change = patch.object(portal, name, value)
            change.start()
            self.addCleanup(change.stop)
        for name, value in {"_owner_exists": False, "_panel_health": True, "_admin_session": False}.items():
            change = patch.object(portal, name, return_value=value)
            change.start()
            self.addCleanup(change.stop)
        portal.STATE = portal.SetupState(self.root / "setup.sqlite3")
        self.token = "K" * 48
        portal.STATE.initialize(self.token, "8.8.8.8")
        portal.STATE.mark_tls_ready("wg-portal-bootstrap", str(portal.TLS_CURRENT))
        portal.app.config["TESTING"] = True
        self.client = portal.app.test_client()
        self.base = "https://8.8.8.8:8767"
        initial = self.client.get("/setup", base_url=self.base)
        self.initial_cookie = initial.headers.get("Set-Cookie", "")
        initial.close()
        with patch.object(portal, "_owner_exists", return_value=False), patch.object(portal, "_panel_health", return_value=True):
            response = self.client.get("/__setup/api/status", base_url=self.base)
        self.csrf = response.json["csrf"]

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data=None, **headers):
        return self.client.post(
            "/__setup/api/" + path,
            base_url=self.base,
            json=data or {},
            headers={"Origin": self.base, "X-Setup-CSRF": self.csrf, **headers},
        )

    def test_setup_page_is_noindex_and_security_headers_are_present(self):
        response = self.client.get("/setup", base_url=self.base)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<html", response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("HttpOnly", self.initial_cookie)
        self.assertIn("Secure", self.initial_cookie)
        response.close()

    def test_plain_http_is_rejected_before_rendering_secret_form(self):
        response = self.client.get("/setup", base_url="http://8.8.8.8:8767")
        self.assertEqual(response.status_code, 400)

    def test_unknown_host_is_rejected(self):
        response = self.client.get("/setup", base_url="https://attacker.example:8767")
        self.assertEqual(response.status_code, 400)

    def test_mutation_requires_same_origin_and_csrf(self):
        response = self.client.post("/__setup/api/owner", base_url=self.base, json={"username": "a@example.com"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/__setup/api/owner", base_url=self.base,
            headers={"Origin": "https://attacker.example", "X-Setup-CSRF": self.csrf},
            json={"username": "a@example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_owner_claim_calls_native_panel_and_replay_is_refused(self):
        payload = {"token": self.token, "username": "owner@example.com", "password": "long-secure-password", "password2": "long-secure-password"}
        with patch.object(portal, "_owner_exists", return_value=False), patch.object(portal, "_register_panel_owner", return_value=True) as register:
            result = self.post("owner", payload)
            self.assertEqual(result.status_code, 200)
            register.assert_called_once()
            replay = self.post("owner", payload)
        self.assertEqual(replay.status_code, 409)
        self.assertTrue(portal.STATE.read()["owner_claimed"])

    def test_owner_password_and_token_validation_happen_before_upstream(self):
        with patch.object(portal, "_owner_exists", return_value=False), patch.object(portal, "_register_panel_owner") as register:
            response = self.post("owner", {"token": self.token, "username": "owner@example.com", "password": "short", "password2": "short"})
        self.assertEqual(response.status_code, 400)
        register.assert_not_called()

    def test_domain_requires_authenticated_admin_and_https_csrf(self):
        with patch.object(portal, "_admin_session", return_value=False):
            response = self.post("domain", {"domain": "vpn.example.com"})
        self.assertEqual(response.status_code, 401)

    def test_completion_requires_logged_in_admin_and_panel_health(self):
        with patch.object(portal, "_admin_session", return_value=False):
            self.assertEqual(self.post("complete").status_code, 401)
        claim = portal.STATE.begin_owner_claim(self.token, "owner@example.com")
        portal.STATE.complete_owner_claim(claim["claim_id"])
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_panel_health", return_value=False):
            self.assertEqual(self.post("complete").status_code, 503)
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_panel_health", return_value=True):
            response = self.post("complete")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(portal.STATE.read()["phase"], "complete")
        closed = self.client.get("/setup", base_url=self.base)
        self.assertEqual(closed.status_code, 303)
        closed.close()
        self.assertEqual(self.client.get("/__setup/api/status", base_url=self.base).status_code, 404)

    def test_health_is_not_equivalent_to_static_http_200(self):
        with patch.object(portal, "_panel_health", return_value=False):
            response = self.client.get("/__setup/health", base_url=self.base)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])

    def owner_exists(self, complete=False):
        claim = portal.STATE.begin_owner_claim(self.token, "owner@example.com")
        portal.STATE.complete_owner_claim(claim["claim_id"])
        if complete:
            portal.STATE.complete_setup(panel_ready=True)

    def manage_post(self, data, path="certificate"):
        return self.client.post("/__manage/api/" + path, base_url=self.base, json=data,
                                headers={"Origin": self.base, "X-Setup-CSRF": self.csrf})

    def test_setup_cookie_is_distinct_from_native_session(self):
        self.assertIn("wgportal_setup_session=", self.initial_cookie)
        self.assertFalse(self.initial_cookie.startswith("session="))

    def test_bootstrap_status_is_honest_and_does_not_set_hsts(self):
        response = self.client.get("/__setup/api/status", base_url=self.base)
        self.assertEqual(response.json["certificate_kind"], "bootstrap")
        self.assertFalse(response.json["tls_trusted"])
        self.assertNotIn("Strict-Transport-Security", response.headers)
        health = self.client.get("/__setup/health", base_url=self.base)
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json["bootstrap_tls"])
        self.assertFalse(health.json["tls_trusted"])

    def test_root_redirects_to_setup_before_completion(self):
        response = self.client.get("/", base_url=self.base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/setup")

    def test_management_requires_native_admin_after_setup(self):
        self.owner_exists(complete=True)
        self.assertEqual(self.client.get("/__setup/api/status", base_url=self.base).status_code, 404)
        self.assertEqual(self.client.get("/__manage/api/status", base_url=self.base).status_code, 401)
        response = self.client.get("/manage", base_url=self.base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/login?next=/manage")
        with patch.object(portal, "_admin_session", return_value=True):
            status = self.client.get("/__manage/api/status", base_url=self.base)
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json["setup_complete"])
        self.assertTrue(status.json["admin_authenticated"])
        self.assertNotIn(self.token, status.text)

    def test_management_mutations_need_csrf_even_with_admin_session(self):
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_issue_manager") as manager:
            response = self.client.post("/__manage/api/certificate", base_url=self.base,
                                        json={"kind": "ip"}, headers={"Origin": self.base})
        self.assertEqual(response.status_code, 403)
        manager.assert_not_called()

    def test_invalid_certificate_kind_is_a_client_error(self):
        with patch.object(portal, "_admin_session", return_value=True):
            self.assertEqual(self.manage_post({"kind": ["ip"]}).status_code, 400)

    def test_failed_domain_issuance_retains_existing_domain_and_removes_candidate(self):
        self.owner_exists()
        portal.STATE.mark_tls_ready("wg-portal-domain", "previous", domain="old.example.com")
        portal.STATE.complete_setup(panel_ready=True)
        manager = Mock()
        manager.issue_domain.side_effect = RuntimeError("authority unavailable")
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_require_helper"), \
             patch.object(portal, "validate_domain_target"), patch.object(portal, "_issue_manager", return_value=manager):
            response = self.manage_post({"kind": "domain", "domain": "new.example.com"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(portal.STATE.read()["domain"], "old.example.com")
        self.assertNotIn("new.example.com", portal._allowed_hosts())
        self.assertFalse(portal.PENDING_HOST.exists())

    def test_missing_reload_helper_prevents_certificate_issuance(self):
        self.owner_exists(complete=True)
        with patch.object(portal, "_admin_session", return_value=True), \
             patch.object(portal, "_require_helper", side_effect=RuntimeError("missing")), \
             patch.object(portal, "_issue_manager") as manager:
            response = self.manage_post({"kind": "ip"})
        self.assertEqual(response.status_code, 502)
        manager.assert_not_called()
        self.assertEqual(portal.STATE.read()["cert_name"], "wg-portal-bootstrap")

    def test_certificate_failure_restores_prior_selection_env_and_state(self):
        from test_acme_manager import write_test_cert
        self.owner_exists(complete=True)
        previous = self.root / "previous"
        previous.mkdir()
        (previous / "fullchain.pem").write_text("previous certificate", encoding="ascii")
        portal.TLS_CURRENT.parent.mkdir()
        try:
            portal.TLS_CURRENT.symlink_to(previous, target_is_directory=True)
        except OSError:
            self.skipTest("This host does not permit symbolic links")
        portal.PORTAL_ENV.write_bytes(b"OTHER_SETTING=preserve\n")
        write_test_cert(self.root / "candidate", "wg-portal-ip", "8.8.8.8")
        certificate = portal.Certificate("wg-portal-ip", "8.8.8.8", self.root / "candidate/wg-portal-ip")
        manager = Mock()
        manager.issue_ip.return_value = certificate
        with patch.object(portal, "_admin_session", return_value=True), \
             patch.object(portal, "_require_helper"), patch.object(portal, "_issue_manager", return_value=manager), \
             patch.object(portal, "_schedule_reload", side_effect=OSError("reload failed")):
            response = self.manage_post({"kind": "ip"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(portal.TLS_CURRENT.resolve(), previous.resolve())
        self.assertEqual(portal.PORTAL_ENV.read_bytes(), b"OTHER_SETTING=preserve\n")
        self.assertEqual(portal.STATE.read()["cert_name"], "wg-portal-bootstrap")
        self.assertEqual(portal.STATE.read()["phase"], "complete")

    def test_certificate_management_succeeds_after_setup_is_locked(self):
        self.owner_exists(complete=True)
        manager = Mock()
        manager.issue_ip.return_value = portal.Certificate("wg-portal-ip", "8.8.8.8", self.root / "unused")
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_require_helper"), \
             patch.object(portal, "_issue_manager", return_value=manager), \
             patch.object(portal, "_stage_certificate", return_value=self.root / "candidate"), patch.object(portal, "_select_directory"), \
             patch.object(portal, "_schedule_reload"):
            response = self.manage_post({"kind": "ip"})
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json["tls_trusted"])
        self.assertEqual(response.json["tls_apply"]["phase"], "pending")
        self.assertEqual(portal.STATE.read()["phase"], "complete")
        self.assertEqual(portal.STATE.read()["cert_name"], "wg-portal-ip")
        journal = portal._read_tls_journal()
        with patch.object(portal, "_admin_session", return_value=True):
            status = self.client.get("/__manage/api/status", base_url=self.base)
        self.assertEqual(status.json["certificate_kind"], "bootstrap")
        self.assertFalse(status.json["tls_trusted"])
        self.assertNotIn("Strict-Transport-Security", status.headers)
        self.assertNotIn("env_b64", status.text)
        self.assertNotIn("target", status.text)
        portal._write_tls_journal({**journal, "phase": "applied"})
        with patch.object(portal, "_admin_session", return_value=True):
            status = self.client.get("/__manage/api/status", base_url=self.base)
        self.assertTrue(status.json["tls_trusted"])
        self.assertEqual(status.json["tls_apply"]["phase"], "applied")

    def test_pending_certificate_reload_blocks_second_issuance(self):
        self.owner_exists(complete=True)
        portal._write_tls_journal({"phase": "pending", "previous": {"tls_state": {}}, "candidate": {}})
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_require_helper"), \
             patch.object(portal, "_issue_manager") as manager:
            response = self.manage_post({"kind": "ip"})
        self.assertEqual(response.status_code, 409)
        manager.assert_not_called()

    def test_tls_recovery_error_is_not_reported_as_trusted(self):
        self.owner_exists(complete=True)
        portal.STATE.mark_tls_ready("wg-portal-ip", "current", allow_complete=True)
        portal._write_tls_journal({"phase": "error", "rollback_ok": False})
        with patch.object(portal, "_admin_session", return_value=True):
            status = self.client.get("/__manage/api/status", base_url=self.base)
        self.assertFalse(status.json["tls_trusted"])
        self.assertTrue(status.json["tls_apply"]["recovery_required"])
        self.assertNotIn("Strict-Transport-Security", status.headers)
        self.assertEqual(self.client.get("/__setup/health", base_url=self.base).status_code, 503)
        with patch.object(portal, "_admin_session", return_value=True), patch.object(portal, "_require_helper"), \
             patch.object(portal, "_issue_manager") as manager:
            self.assertEqual(self.manage_post({"kind": "ip"}).status_code, 503)
        manager.assert_not_called()

    def test_helper_rejects_writable_or_symlink_ancestor(self):
        safe = Mock(st_uid=0, st_mode=0o755)
        helper = Mock()
        helper.is_symlink.return_value = False
        helper.is_file.return_value = True
        helper.stat.return_value = safe
        ancestor = Mock()
        ancestor.is_symlink.return_value = True
        helper.parents = [ancestor]
        with patch.object(portal.os, "access", return_value=True):
            with self.assertRaises(RuntimeError):
                portal._require_helper(helper)

    def test_native_registration_uses_internal_token_and_real_field_names(self):
        transport = Mock()
        transport.get.return_value = Mock(status_code=200, text='<input value="signed-csrf" name="csrf_token">')
        transport.post.return_value = Mock(status_code=302, headers={"Location": "/login"})
        with patch.object(portal.requests, "Session", return_value=transport):
            self.assertTrue(portal._register_panel_owner("owner@example.com", "long-password-value", "claim-token"))
        payload = transport.post.call_args.kwargs["data"]
        self.assertEqual(payload["setup_token"], os.environ["SETUP_TOKEN"])
        self.assertEqual(payload["password2"], payload["password"])
        self.assertEqual(payload["csrf_token"], "signed-csrf")

    def test_uninitialized_storage_returns_recovery_response(self):
        with patch.object(portal, "STATE", portal.SetupState(self.root / "missing.sqlite3")):
            self.assertEqual(self.client.get("/setup", base_url=self.base).status_code, 503)
            self.assertEqual(self.post("owner", {}).status_code, 503)

    def test_native_csp_and_large_body_are_preserved(self):
        from urllib3._collections import HTTPHeaderDict
        upstream = Mock(status_code=200)
        upstream.headers = HTTPHeaderDict({"Content-Security-Policy": "script-src 'self' 'unsafe-inline'"})
        upstream.raw.headers = upstream.headers
        upstream.raw.stream.return_value = iter([b"native response"])
        transport = Mock()
        transport.request.return_value = upstream
        with patch.object(portal.requests, "Session", return_value=transport):
            response = self.client.post("/register", base_url=self.base, data=b"x" * 40000)
            self.assertEqual(response.status_code, 200)
            self.assertIn("'unsafe-inline'", response.headers["Content-Security-Policy"])
            self.assertEqual(len(transport.request.call_args.kwargs["data"]), 40000)
            response.close()

    def test_telegram_start_is_admin_only_and_uses_fixed_helper(self):
        with patch.object(portal, "_run_helper") as run:
            self.assertEqual(self.manage_post({}, "telegram/start").status_code, 401)
            run.assert_not_called()
            with patch.object(portal, "_admin_session", return_value=True):
                response = self.manage_post({}, "telegram/start")
        self.assertEqual(response.status_code, 200)
        run.assert_called_once_with(portal.TELEGRAM_HELPER)


if __name__ == "__main__":
    unittest.main()
