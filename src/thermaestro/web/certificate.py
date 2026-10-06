"""The installation's own HTTPS certificate: self-signed, made on first start.

A certificate for a LAN address can't come from a public authority, so each
installation makes its own. It encrypts, but the browser warns about it; the web UI
shows its SHA-256 fingerprint so a careful user can compare it with the browser's. It is
made again a month before it expires, which changes the fingerprint.
"""

import ipaddress
import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ..files import UnsafePath, check_private_file, private_directory, write_private

log = logging.getLogger(__name__)

LIFETIME = timedelta(days=825)
RENEW_BEFORE = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Certificate:
    cert: Path
    key: Path
    fingerprint: str
    """SHA-256 of the certificate, as browsers show it: hex pairs and colons."""


def fingerprint(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def ensure(directory: Path, now: datetime | None = None) -> Certificate:
    """The certificate in `directory`, made (again) when there is none or it is about
    to expire."""
    now = now or datetime.now(UTC)
    private_directory(directory)
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    try:
        check_private_file(key_path)
        current = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (FileNotFoundError, UnsafePath, ValueError):
        current = None
    if current is not None and current.not_valid_after_utc - now > RENEW_BEFORE:
        return Certificate(cert_path, key_path, fingerprint(current))
    key, certificate = _make(now)
    write_private(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    write_private(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
    log.info("made a new HTTPS certificate, SHA-256 %s", fingerprint(certificate))
    return Certificate(cert_path, key_path, fingerprint(certificate))


def _make(now: datetime) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "thermaestro"
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, host[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Thermaestro (self-signed)"),
        ]
    )
    names: list[x509.GeneralName] = [
        x509.DNSName(host),
        x509.DNSName(f"{host}.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + LIFETIME)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return key, certificate
