import importlib.util
import os
import base64
import json
import stat
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace


LINUX = Path(__file__).resolve().parents[1] / "deploy" / "linux"
sys.path.insert(0, str(LINUX))
SPEC = importlib.util.spec_from_file_location("tls_service", LINUX / "service_helpers" / "tls_service.py")
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)
REPLACE = os.replace


def test_replace(source, destination):
    # Windows cannot replace directory symlinks. Adapt only the test filesystem operation;
    # Linux atomic rename semantics still require the separately gated Linux integration run.
    if os.name == "nt" and Path(destination).is_symlink():
        Path(destination).unlink()
    REPLACE(source, destination)


class ServiceBoundaryTests(unittest.TestCase):
    def test_renewal_is_scoped_to_this_installation_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service.subprocess, "run") as run, patch.object(service, "_renew_reload_locked"):
            root = Path(directory)
            service.renew(root)
            command = run.call_args.args[0]
            for option, suffix in (("--config-dir", "acme-config"), ("--work-dir", "acme-work"), ("--logs-dir", "acme-logs")):
                self.assertEqual(command[command.index(option) + 1], str(root / "runtime" / suffix))
            self.assertEqual(command[0], str(root / "acme-venv" / "bin" / "certbot"))
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertNotIn("--deploy-hook", command)
            self.assertEqual(run.call_args.kwargs["env"]["WG_PORTAL_PARENT_RENEWAL"], "1")

    def test_telegram_refuses_an_unrelated_service_without_running_systemctl(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service.subprocess, "run") as run:
            root = Path(directory)
            unit = root / "other.service"
            unit.write_text("[Service]\nWorkingDirectory=/other/install\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                service.start_telegram(root, unit)
            run.assert_not_called()

    def test_telegram_starts_only_owned_fixed_unit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service.subprocess, "run") as run:
            root = Path(directory)
            unit = root / "own.service"
            unit.write_text("[Service]\nWorkingDirectory=" + str(root / "vendor" / "WG_Panel") + "\n", encoding="utf-8")
            unit.chmod(0o600)
            service.start_telegram(root, unit)
            run.assert_called_once_with(["systemctl", "enable", "--now", "wg-panel-bot.service"], check=True, timeout=60)

    def _certificate_fixture(self, root):
        try:
            import cryptography
        except ImportError:
            self.skipTest("Certificate integration requires the existing crypto runtime")
        import bootstrap
        from setup_state import SetupState
        runtime = root / "runtime"
        current, _ = bootstrap.bootstrap_certificate(runtime, "8.8.8.8")
        state = SetupState(runtime / "setup.sqlite3")
        state.initialize("c" * 40, "8.8.8.8")
        state.mark_tls_ready("wg-portal-ip", str(current))
        source = runtime / "acme-config/live/wg-portal-ip"
        source.mkdir(parents=True)
        replacement, _ = bootstrap.bootstrap_certificate(root / "new-certificate", "8.8.8.8")
        for filename in ("fullchain.pem", "privkey.pem"):
            (source / filename).write_bytes((replacement / filename).read_bytes())
        return current

    def test_renewal_snapshots_certificate_before_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._certificate_fixture(root)
            previous = current.resolve()
            with patch.object(service, "_restart_selected") as reload, patch.object(service.os, "replace", side_effect=test_replace):
                service.renew_reload_tls(root)
            self.assertNotEqual(previous, current.resolve())
            self.assertTrue(current.resolve().is_relative_to((root / "runtime/tls/versions").resolve()))
            self.assertTrue((previous / "privkey.pem").is_file())
            reload.assert_called_once_with(root)
            self.assertEqual(service._read_journal(root)["phase"], "applied")

    def test_failed_tls_restart_restores_previous_certificate_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._certificate_fixture(root)
            previous = current.resolve()
            with patch.object(service, "_restart_selected", side_effect=[RuntimeError("restart failed"), None]) as reload, patch.object(service.os, "replace", side_effect=test_replace):
                with self.assertRaises(RuntimeError):
                    service.renew_reload_tls(root)
            self.assertEqual(current.resolve(), previous)
            self.assertEqual(reload.call_count, 2)
            journal = json.loads((root / "runtime/tls/apply-journal.json").read_text())
            self.assertEqual(journal["phase"], "error")
            self.assertTrue(journal["rollback_ok"])

    def test_writable_or_foreign_privileged_metadata_is_refused(self):
        path = Path("checkout")
        service.assert_secure_metadata(path, SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755))
        for owner, mode in ((1000, 0o755), (0, 0o775), (0, 0o777)):
            with self.assertRaises(ValueError):
                service.assert_secure_metadata(path, SimpleNamespace(st_uid=owner, st_mode=stat.S_IFDIR | mode))

    def test_symlinked_ancestry_is_refused_before_metadata_or_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "actual"
            target.mkdir()
            link = root / "symlink"
            link.symlink_to(target, target_is_directory=True)
            with patch.object(service, "assert_secure_metadata"):
                with self.assertRaisesRegex(ValueError, "Symlinked"):
                    service.assert_secure_ancestry(link / "not-created", permit_missing=True)

    def test_pending_tls_operation_defers_renewal_without_running_certbot(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service.subprocess, "run") as run:
            root = Path(directory)
            service._write_journal(root, {"version": 1, "phase": "pending"})
            service.renew(root)
            run.assert_not_called()

    def test_interrupted_selection_is_rolled_back_instead_of_reported_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._certificate_fixture(root)
            previous = current.resolve()
            state = service._state(root).read()
            env = root / "runtime/portal.env"
            original = b"# before\nKEEP=private\n"
            env.write_bytes(b"CANDIDATE=changed\n")
            journal = {"version": 1, "operation_id": "interrupted", "phase": "pending",
                       "previous": {"target": str(previous), "env_b64": base64.b64encode(original).decode(),
                                    "tls_state": {key: state.get(key) for key in ("domain", "tls_ready", "cert_name", "cert_path")}},
                       "candidate": {"target": str(root / "runtime/acme-config/live/wg-portal-ip"),
                                     "cert_name": "wg-portal-ip", "domain": None}}
            service._write_journal(root, journal)
            with patch.object(service, "_restart_selected") as restart, patch.object(service.os, "replace", side_effect=test_replace):
                with self.assertRaises(RuntimeError):
                    service.reload_tls(root, pending_only=True)
            self.assertEqual(current.resolve(), previous)
            self.assertEqual(env.read_bytes(), original)
            restart.assert_called_once_with(root)
            final = service._read_journal(root)
            self.assertEqual(final["phase"], "error")
            self.assertTrue(final["rollback_ok"])

    def test_failed_previous_restart_reports_recovery_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._certificate_fixture(root)
            with patch.object(service, "_restart_selected", side_effect=RuntimeError("service down")), patch.object(service.os, "replace", side_effect=test_replace):
                with self.assertRaises(RuntimeError):
                    service.renew_reload_tls(root)
            self.assertFalse(service._read_journal(root)["rollback_ok"])

    def test_served_certificate_mismatch_prevents_health_success(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._certificate_fixture(root)
            state = service._state(root).read()
            connection, secured, context = MagicMock(), MagicMock(), MagicMock()
            secured.__enter__.return_value = secured
            secured.getpeercert.return_value = b"different-server-certificate"
            context.wrap_socket.return_value = secured
            with patch.object(service.socket, "create_connection", return_value=connection), patch.object(service.ssl, "SSLContext", return_value=context), patch.object(service.http.client, "HTTPSConnection") as health:
                with self.assertRaisesRegex(ValueError, "not serving"):
                    service._check_served_certificate(root, state, current.resolve())
                health.assert_not_called()


if __name__ == "__main__":
    unittest.main()
