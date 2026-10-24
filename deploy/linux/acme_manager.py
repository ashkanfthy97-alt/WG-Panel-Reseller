"""Narrow ACME orchestration for the setup service; never invokes a shell."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import re
import socket
import subprocess


class AcmeError(Exception):
    pass


class InvalidDomain(AcmeError):
    pass


class DnsTargetMismatch(AcmeError):
    pass


class CertificateIssueFailed(AcmeError):
    pass


class CertbotUnsupported(AcmeError):
    pass


@dataclass(frozen=True)
class Certificate:
    name: str
    domain: str
    directory: Path

    @property
    def fullchain(self) -> Path:
        return self.directory / "fullchain.pem"

    @property
    def private_key(self) -> Path:
        return self.directory / "privkey.pem"


def normalize_domain(value: str) -> str:
    raw = (value or "").strip().rstrip(".")
    if not raw or "://" in raw or "/" in raw or "*" in raw or "@" in raw:
        raise InvalidDomain("Enter a domain name only, such as vpn.example.com")
    try:
        ipaddress.ip_address(raw.strip("[]"))
    except ValueError:
        pass
    else:
        raise InvalidDomain("Enter a DNS name; the VPS IP is already covered by its IP certificate")
    try:
        domain = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidDomain("The domain name is not valid") from exc
    if len(domain) > 253 or "." not in domain:
        raise InvalidDomain("Use a fully qualified domain name")
    labels = domain.split(".")
    if any(not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        raise InvalidDomain("The domain name contains an invalid label")
    return domain


def validate_domain_target(domain: str, public_ip: str, resolver=socket.getaddrinfo) -> list[str]:
    name = normalize_domain(domain)
    target = str(ipaddress.ip_address(public_ip))
    try:
        resolved = resolver(name, 80, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DnsTargetMismatch("The domain does not currently resolve to an IP address") from exc
    addresses = sorted({str(ipaddress.ip_address(item[4][0])) for item in resolved})
    if not addresses or addresses != [target]:
        raise DnsTargetMismatch(
            f"The domain must resolve only to this VPS IP ({target}); DNS currently returns: {', '.join(addresses) or 'none'}"
        )
    return addresses


class CertificateManager:
    def __init__(
        self,
        executable: str,
        webroot: Path,
        cert_root: Path,
        runner=None,
        timeout: int = 180,
        config_dir: Path | None = None,
        work_dir: Path | None = None,
        logs_dir: Path | None = None,
    ):
        self.executable = executable
        self.webroot = Path(webroot)
        self.cert_root = Path(cert_root)
        self.runner = runner or self._run
        self.timeout = timeout
        self.config_dir = Path(config_dir) if config_dir else self.cert_root.parent
        self.work_dir = Path(work_dir) if work_dir else self.config_dir.parent / "acme-work"
        self.logs_dir = Path(logs_dir) if logs_dir else self.config_dir.parent / "acme-logs"

    def _directory_args(self) -> list[str]:
        return ["--config-dir", str(self.config_dir), "--work-dir", str(self.work_dir),
                "--logs-dir", str(self.logs_dir)]

    def _run(self, argv, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)

    def require_ip_capability(self) -> None:
        try:
            result = self.runner([self.executable, "--version"], shell=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CertbotUnsupported("The isolated Certbot runtime is not available") from exc
        output = " ".join((getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or ""))
        if isinstance(result, int):
            output = ""
        match = re.search(r"certbot\s+(\d+)\.(\d+)(?:\.(\d+))?", output, re.I)
        if not match or tuple(int(part or 0) for part in match.groups()) < (5, 4, 0):
            raise CertbotUnsupported("Certbot 5.4 or newer is required for trusted IP-address certificates")

    def issue_ip(self, public_ip: str) -> Certificate:
        try:
            address = ipaddress.ip_address(public_ip)
        except ValueError as exc:
            raise ValueError("A valid public IP address is required") from exc
        if not address.is_global:
            raise ValueError("A globally routable public IP address is required for ACME")
        self.require_ip_capability()
        return self._issue(
            "wg-portal-ip",
            str(address),
            ["--ip-address", str(address), "--preferred-profile", "shortlived"],
        )

    def issue_domain(self, domain: str, public_ip: str | None = None) -> Certificate:
        name = normalize_domain(domain)
        args = ["-d", name]
        additional_names = []
        if public_ip:
            address = ipaddress.ip_address(public_ip)
            if not address.is_global:
                raise ValueError("A globally routable public IP address is required for ACME")
            self.require_ip_capability()
            args.extend(["--ip-address", str(address), "--preferred-profile", "shortlived"])
            additional_names.append(str(address))
        return self._issue("wg-portal-domain", name, args, additional_names)

    def _issue(self, cert_name: str, subject: str, subject_args: list[str], additional_names=()) -> Certificate:
        self.webroot.mkdir(mode=0o755, parents=True, exist_ok=True)
        argv = [
            self.executable, "certonly", "--non-interactive", "--agree-tos",
            "--register-unsafely-without-email", "--webroot", "-w", str(self.webroot),
            "--cert-name", cert_name, *self._directory_args(), *subject_args,
        ]
        try:
            result = self.runner(argv, shell=False, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CertificateIssueFailed("Certbot could not reach the certificate authority") from exc
        return_code = result if isinstance(result, int) else getattr(result, "returncode", 1)
        if return_code != 0:
            raise CertificateIssueFailed("Certificate issuance failed; verify DNS, inbound TCP 80, and Certbot logs")
        cert = Certificate(cert_name, subject, self.cert_root / cert_name)
        if not cert.fullchain.is_file() or not cert.private_key.is_file():
            raise CertificateIssueFailed("Certbot returned success but its certificate files are missing")
        try:
            validate_certificate(cert, subject)
            for expected in additional_names:
                validate_certificate(cert, expected)
        except (OSError, ValueError, TypeError) as exc:
            raise CertificateIssueFailed("Issued certificate does not match its requested name and private key") from exc
        return cert

    def renew(self) -> None:
        try:
            result = self.runner(
                [self.executable, "renew", "--non-interactive", *self._directory_args(),
                 "--deploy-hook", "/usr/local/lib/wgportal/reload-setup-tls"],
                shell=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcmeError("Certificate renewal could not be run") from exc
        if (result if isinstance(result, int) else getattr(result, "returncode", 1)) != 0:
            raise AcmeError("Certificate renewal failed; the previously installed certificate remains active")

    @staticmethod
    def status(certificate: Certificate) -> dict:
        if not certificate.fullchain.is_file() or not certificate.private_key.is_file():
            return {"ready": False, "name": certificate.name}
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(certificate.fullchain.read_bytes())
        expires = cert.not_valid_after_utc
        days_remaining = int((expires - datetime.now(timezone.utc)).total_seconds() // 86400)
        return {
            "ready": True,
            "name": certificate.name,
            "subject": cert.subject.rfc4514_string(),
            "not_after": expires.isoformat(),
            "days_remaining": days_remaining,
        }


def validate_certificate(certificate: Certificate, expected_name: str, minimum_hours: int = 24) -> dict:
    """Check SAN, expiry and private-key correspondence before selecting a cert."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(certificate.fullchain.read_bytes())
    key = serialization.load_pem_private_key(certificate.private_key.read_bytes(), password=None)
    cert_pub = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_pub != key_pub:
        raise ValueError("Certificate public key does not match the private key")
    if cert.not_valid_before_utc > datetime.now(timezone.utc):
        raise ValueError("Certificate is not valid yet")
    if (cert.not_valid_after_utc - datetime.now(timezone.utc)).total_seconds() < minimum_hours * 3600:
        raise ValueError("Certificate is too close to expiration")
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    try:
        expected_ip = ipaddress.ip_address(expected_name)
    except ValueError:
        if expected_name.lower() not in {name.lower() for name in san.get_values_for_type(x509.DNSName)}:
            raise ValueError("Certificate does not contain the requested DNS name")
    else:
        if expected_ip not in san.get_values_for_type(x509.IPAddress):
            raise ValueError("Certificate does not contain the requested IP address")
    return CertificateManager.status(certificate)
