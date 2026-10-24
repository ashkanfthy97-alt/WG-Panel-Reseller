import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_helpers", ROOT / "deploy/linux/install_helpers.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class EnvMigrationChecks(unittest.TestCase):
    def test_maps_legacy_bind_without_removing_original_value(self):
        source = "WG_PANEL_BIND=127.0.0.1:8866\nFLASK_SECRET_KEY=unchanged\n"
        migrated, port = module.migrate_env_text(source, requested_port=8766)
        self.assertIn("WG_PANEL_BIND=127.0.0.1:8866\n", migrated)
        self.assertIn("BIND=127.0.0.1:8866\n", migrated)
        self.assertIn("FLASK_SECRET_KEY=unchanged\n", migrated)
        self.assertEqual(port, 8866)

    def test_current_bind_wins_and_is_preserved(self):
        source = "# retained\nBIND=127.0.0.1:8766\n"
        migrated, port = module.migrate_env_text(source, requested_port=8766)
        self.assertEqual(migrated, source)
        self.assertEqual(port, 8766)

    def test_explicit_conflicting_port_fails_without_modifying_source(self):
        source = "WG_PANEL_BIND=127.0.0.1:8866\n"
        with self.assertRaises(ValueError):
            module.migrate_env_text(source, requested_port=8766, port_explicit=True)
        self.assertEqual(source, "WG_PANEL_BIND=127.0.0.1:8866\n")

    def test_refuses_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            module.migrate_env_text("BIND=0.0.0.0:8766\n", requested_port=8766)

    def test_refuses_conflicting_duplicate_bind_keys(self):
        with self.assertRaises(ValueError):
            module.migrate_env_text(
                "BIND=127.0.0.1:8766\nBIND=127.0.0.1:8866\n", requested_port=8766
            )

    def test_supported_runtime_is_explicit(self):
        self.assertIsNone(module.runtime_error((3, 12), "Linux", "x86_64"))
        self.assertIn("3.12", module.runtime_error((3, 11), "Linux", "x86_64"))
        self.assertIn("3.13", module.runtime_error((3, 13), "Linux", "x86_64"))
        self.assertIn("3.14", module.runtime_error((3, 14), "Linux", "x86_64"))
        self.assertIn("x86_64", module.runtime_error((3, 12), "Linux", "aarch64"))

    def test_fallback_env_is_copied_not_moved_and_keeps_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.env"
            target = root / "vendor" / ".env"
            original = "WG_PANEL_BIND=127.0.0.1:8866\nFERNET_KEY=keep-this\n"
            source.write_text(original, encoding="utf-8")
            port = module.migrate_env_file(target, source, 8766, False)
            self.assertEqual(port, 8866)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertIn("FERNET_KEY=keep-this\n", target.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_env_update_preserves_unrelated_values_and_comments(self):
        original = "# keep me\nAPI_KEY=existing-secret\nSETUP_TOKEN=old\n"
        updated = module.update_env_text(original, {"SETUP_TOKEN": "a" * 48, "SECURE_COOKIES": "1"})
        self.assertIn("# keep me\n", updated)
        self.assertIn("API_KEY=existing-secret\n", updated)
        self.assertIn("SETUP_TOKEN=" + "a" * 48 + "\n", updated)
        self.assertIn("SECURE_COOKIES=1\n", updated)

    def test_env_update_refuses_duplicate_conflicting_secret(self):
        with self.assertRaises(ValueError):
            module.update_env_text("SETUP_TOKEN=one\nSETUP_TOKEN=two\n", {"SETUP_TOKEN": "new"})

    def test_env_update_rejects_newline_in_values(self):
        with self.assertRaises(ValueError):
            module.update_env_text("", {"SETUP_TOKEN": "safe\nINJECTED=value"})


class DependencyLockChecks(unittest.TestCase):
    def test_every_exact_dependency_pin_has_a_sha256_hash(self):
        lock = (ROOT / "deploy/linux/requirements.lock").read_text(encoding="utf-8").splitlines()
        pins = [index for index, line in enumerate(lock) if "==" in line and not line.lstrip().startswith("#")]
        self.assertEqual(len(pins), 44)
        for index in pins:
            self.assertTrue(lock[index].endswith("\\"), lock[index])
            self.assertRegex(lock[index + 1], r"^\s+--hash=sha256:[0-9a-f]{64}$")

    def test_existing_env_is_backed_up_before_atomic_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".env"
            original = "WG_PANEL_BIND=127.0.0.1:8866\nAPI_KEY=unchanged\n"
            target.write_text(original, encoding="utf-8")
            port = module.migrate_env_file(target, None, 8766, False)
            self.assertEqual(port, 8866)
            self.assertEqual(target.with_name(".env.pre-wg-portal").read_text(encoding="utf-8"), original)
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
