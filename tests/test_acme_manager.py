import importlib.util
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "acme_manager", ROOT / "deploy/linux/acme_manager.py"
)
acme = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acme
SPEC.loader.exec_module(acme)


def write_test_cert(root: Path, name: str, san_value: str, extra_names=()):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID
    key = ed25519.Ed25519PrivateKey.generate()
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    def san_name(value):
        try:
            return x509.IPAddress(ipaddress.ip_address(value))
        except ValueError:
            return x509.DNSName(value)
    names = [san_name(value) for value in (san_value, *extra_names)]
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, algorithm=None)
    )
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "privkey.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))


class AcmeManagerTests(unittest.TestCase):
    def test_domain_validation_rejects_url_wildcard_and_localhost(self):
        for value in ("https://example.com", "*.example.com", "localhost", "bad..example"):
            with self.subTest(value=value), self.assertRaises(acme.InvalidDomain):
                acme.normalize_domain(value)

    def test_domain_validation_normalizes_case_and_trailing_dot(self):
        self.assertEqual(acme.normalize_domain("Vpn.Example.COM."), "vpn.example.com")

    def test_dns_must_match_server_ip(self):
        resolver = lambda *_args, **_kwargs: [("vpn.example.com", [], 0, 0, ("198.51.100.42", 0))]
        self.assertEqual(acme.validate_domain_target("vpn.example.com", "198.51.100.42", resolver), ["198.51.100.42"])
        wrong = lambda *_args, **_kwargs: [("vpn.example.com", [], 0, 0, ("203.0.113.9", 0))]
        with self.assertRaises(acme.DnsTargetMismatch):
            acme.validate_domain_target("vpn.example.com", "198.51.100.42", wrong)

    def test_ip_certificate_uses_short_lived_profile_and_webroot_without_shell(self):
        calls = []
        temporary = tempfile.TemporaryDirectory()
        cert_root = Path(temporary.name)

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if "--version" in argv:
                return Mock(returncode=0, stdout="certbot 5.4.0", stderr="")
            write_test_cert(cert_root, "wg-portal-ip", "8.8.8.8")
            return 0

        manager = acme.CertificateManager(
            executable="/snap/bin/certbot",
            webroot=cert_root / "webroot",
            cert_root=cert_root,
            runner=runner,
        )
        cert = manager.issue_ip("8.8.8.8")
        args, options = calls[1]
        self.assertIn("--ip-address", args)
        self.assertIn("8.8.8.8", args)
        self.assertIn("shortlived", args)
        self.assertIn("--webroot", args)
        self.assertFalse(options["shell"])
        self.assertEqual(cert.name, "wg-portal-ip")
        temporary.cleanup()

    def test_domain_certificate_validates_domain_and_uses_dns_name(self):
        calls = []
        temporary = tempfile.TemporaryDirectory()
        cert_root = Path(temporary.name)

        def runner(argv, **kwargs):
            calls.append(argv)
            write_test_cert(cert_root, "wg-portal-domain", "vpn.example.com")
            return 0

        manager = acme.CertificateManager(
            executable="certbot", webroot=cert_root / "webroot", cert_root=cert_root,
            runner=runner,
        )
        cert = manager.issue_domain("Vpn.Example.com.")
        self.assertEqual(cert.name, "wg-portal-domain")
        self.assertEqual(cert.domain, "vpn.example.com")
        self.assertIn("-d", calls[0])
        self.assertIn("vpn.example.com", calls[0])
        temporary.cleanup()

    def test_certbot_failure_is_reported_without_claiming_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def runner(argv, **kwargs):
                return Mock(returncode=0, stdout="certbot 5.4.0", stderr="") if "--version" in argv else 1
            manager = acme.CertificateManager("certbot", root / "webroot", root / "live", runner=runner)
            with self.assertRaises(acme.CertificateIssueFailed):
                manager.issue_ip("8.8.8.8")

    def test_certificate_must_match_expected_san_and_key(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        write_test_cert(root, "wg-portal-ip", "1.1.1.1")
        cert = acme.Certificate("wg-portal-ip", "8.8.8.8", root / "wg-portal-ip")
        with self.assertRaises(ValueError):
            acme.validate_certificate(cert, "8.8.8.8")
        status = acme.validate_certificate(cert, "1.1.1.1")
        self.assertTrue(status["ready"])
        self.assertGreater(status["days_remaining"], 0)
        temporary.cleanup()

    def test_ip_issuance_requires_supported_certbot(self):
        result = Mock(returncode=0, stdout="certbot 5.3.0", stderr="")
        manager = acme.CertificateManager("certbot", Path("/tmp/acme"), Path("/tmp/live"), runner=lambda *_a, **_k: result)
        with self.assertRaises(acme.CertbotUnsupported):
            manager.issue_ip("8.8.8.8")

    def test_domain_certificate_preserves_ip_san_and_isolated_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if "--version" in argv:
                    return Mock(returncode=0, stdout="certbot 5.4.0", stderr="")
                write_test_cert(root / "config/live", "wg-portal-domain", "vpn.example.com", ["8.8.8.8"])
                return 0
            manager = acme.CertificateManager(
                "isolated/certbot", root / "webroot", root / "config/live", runner=runner,
                config_dir=root / "config", work_dir=root / "work", logs_dir=root / "logs",
            )
            certificate = manager.issue_domain("vpn.example.com", "8.8.8.8")
            self.assertTrue(acme.validate_certificate(certificate, "8.8.8.8")["ready"])
            argv = calls[-1]
            for option, expected in [("--config-dir", root / "config"), ("--work-dir", root / "work"), ("--logs-dir", root / "logs")]:
                self.assertEqual(argv[argv.index(option) + 1], str(expected))
            self.assertIn("shortlived", argv)

    def test_domain_certificate_cannot_drop_requested_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def runner(argv, **kwargs):
                if "--version" in argv:
                    return Mock(returncode=0, stdout="certbot 5.4.0", stderr="")
                write_test_cert(root, "wg-portal-domain", "vpn.example.com")
                return 0
            manager = acme.CertificateManager("certbot", root / "webroot", root, runner=runner)
            with self.assertRaises(acme.CertificateIssueFailed):
                manager.issue_domain("vpn.example.com", "8.8.8.8")

    def test_subprocess_runner_accepts_explicit_timeout(self):
        from unittest.mock import patch
        manager = acme.CertificateManager("certbot", Path("webroot"), Path("live"))
        with patch.object(acme.subprocess, "run", return_value=Mock(returncode=0)) as run:
            manager._run(["certbot", "--version"], shell=False, timeout=15)
        self.assertEqual(run.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
