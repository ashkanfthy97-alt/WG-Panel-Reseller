import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


LINUX = Path(__file__).resolve().parents[1] / "deploy" / "linux"
sys.path.insert(0, str(LINUX))
SPEC = importlib.util.spec_from_file_location("bootstrap", LINUX / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_env_update_preserves_unknown_lines_and_never_accepts_multiline(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "panel.env"
            env.write_text("# existing\nKEEP=value\nSETUP_TOKEN=old\n", encoding="utf-8")
            bootstrap.update_env(env, {"SETUP_TOKEN": "new value"})
            self.assertIn("# existing\nKEEP=value\n", env.read_text(encoding="utf-8"))
            self.assertEqual(env.read_text(encoding="utf-8").count("SETUP_TOKEN="), 1)
            before = env.read_bytes()
            with self.assertRaises(ValueError):
                bootstrap.update_env(env, {"NEW": "bad\nline"})
            self.assertEqual(env.read_bytes(), before)

    def test_existing_token_never_prompts_or_rewrites(self):
        prompt = Mock(side_effect=AssertionError("must not prompt"))
        with patch.object(bootstrap, "load_env", return_value={"SETUP_TOKEN": "a" * 40}), patch.object(bootstrap, "update_env") as update:
            self.assertEqual(bootstrap.ensure_token(Path("unused"), prompt), "a" * 40)
        update.assert_not_called()

    def test_missing_token_is_requested_once_and_not_printed(self):
        prompt = Mock(return_value="b" * 40)
        output = io.StringIO()
        with patch.object(bootstrap, "load_env", return_value={}), patch.object(bootstrap, "update_env") as update, patch("sys.stdout", output):
            bootstrap.ensure_token(Path("unused"), prompt)
        prompt.assert_called_once_with()
        update.assert_called_once_with(Path("unused"), {"SETUP_TOKEN": "b" * 40})
        self.assertEqual(output.getvalue(), "")

    def test_short_existing_token_is_not_silently_replaced(self):
        with patch.object(bootstrap, "load_env", return_value={"SETUP_TOKEN": "short"}), patch.object(bootstrap, "update_env") as update:
            with self.assertRaises(ValueError):
                bootstrap.ensure_token(Path("unused"), Mock())
        update.assert_not_called()

    def test_whitespace_token_is_refused_without_writing(self):
        with patch.object(bootstrap, 'load_env', return_value={}), patch.object(bootstrap, 'update_env') as update:
            with self.assertRaises(ValueError):
                bootstrap.ensure_token(Path('unused'), lambda: ' ' * 40)
        update.assert_not_called()

    def test_missing_configured_public_certificate_is_not_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {'SETUP_TOKEN': 't' * 40, 'FLASK_SECRET_KEY': 's' * 40, 'API_KEY': 'private-test-key'}
            with patch.object(bootstrap, 'load_env', return_value=values), patch.object(bootstrap, 'detect_public_ip', return_value='8.8.8.8'), patch('setup_state.SetupState') as state, patch.object(bootstrap, 'bootstrap_certificate') as generate:
                state.return_value.read.return_value = {'cert_name': 'wg-portal-ip'}
                with self.assertRaisesRegex(ValueError, 'public certificate is unavailable'):
                    bootstrap.prepare(root, root / 'panel.env', 8766, 8765, 80)
                generate.assert_not_called()

    def test_public_route_prevents_external_echo_request(self):
        runner = Mock(return_value=Mock(stdout=json.dumps([{"prefsrc": "8.8.8.8"}])))
        opener = Mock()
        self.assertEqual(bootstrap.detect_public_ip(runner=runner, opener=opener), "8.8.8.8")
        opener.open.assert_not_called()

    def test_private_route_falls_back_to_bounded_https_response(self):
        runner = Mock(return_value=Mock(stdout=json.dumps([{"prefsrc": "10.0.0.2"}])))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"8.8.4.4\n"
        opener = Mock()
        opener.open.return_value = response
        self.assertEqual(bootstrap.detect_public_ip(runner=runner, opener=opener), "8.8.4.4")
        response.read.assert_called_once_with(128)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 8)

    def test_invalid_explicit_ip_does_not_fallback_to_another_address(self):
        runner, opener = Mock(), Mock()
        with self.assertRaises(ValueError):
            bootstrap.detect_public_ip("127.0.0.1", runner, opener)
        runner.assert_not_called()
        opener.open.assert_not_called()

    def test_real_certificate_is_reused_and_public_summary_has_no_secrets(self):
        try:
            import cryptography
        except ImportError:
            self.skipTest("Certificate integration requires the existing crypto runtime")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel_env = root / "panel.env"
            token = "claim-" + "a" * 40
            values = {"SETUP_TOKEN": token, "FLASK_SECRET_KEY": "f" * 48, "API_KEY": "api-private", "UNKNOWN": "preserve"}
            bootstrap.update_env(panel_env, values)
            original_env = panel_env.read_bytes()
            # The existing review runtime lacks dotenv; exercise real state and crypto with an injected env reader.
            with patch.object(bootstrap, "detect_public_ip", return_value="8.8.8.8"), patch.object(bootstrap, "load_env", side_effect=lambda path: values if path == panel_env else {}):
                first = bootstrap.prepare(root, panel_env, 8766, 8765, 80)
                current = (root / "runtime/tls/current").resolve()
                second = bootstrap.prepare(root, panel_env, 8766, 8765, 80)
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(current, (root / "runtime/tls/current").resolve())
            summary = (root / "runtime/bootstrap-summary.json").read_text(encoding="utf-8")
            self.assertNotIn(token, summary)
            self.assertNotIn("api-private", summary)
            self.assertEqual(panel_env.read_bytes(), original_env)


if __name__ == "__main__":
    unittest.main()
