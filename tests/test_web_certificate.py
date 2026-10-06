import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509

from thermaestro.web import certificate


def test_made_once_then_kept(tmp_path: Path) -> None:
    first = certificate.ensure(tmp_path / "tls")
    assert stat.S_IMODE(first.key.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "tls").stat().st_mode) == 0o700
    again = certificate.ensure(tmp_path / "tls")
    assert again.fingerprint == first.fingerprint
    assert len(first.fingerprint.split(":")) == 32


def test_renewed_a_month_before_it_expires(tmp_path: Path) -> None:
    first = certificate.ensure(tmp_path)
    made = x509.load_pem_x509_certificate(first.cert.read_bytes())
    assert made.not_valid_after_utc - made.not_valid_before_utc > timedelta(days=800)
    later = datetime.now(UTC) + certificate.LIFETIME - timedelta(days=29)
    renewed = certificate.ensure(tmp_path, now=later)
    assert renewed.fingerprint != first.fingerprint


def test_a_server_certificate_for_this_host(tmp_path: Path) -> None:
    made = x509.load_pem_x509_certificate(certificate.ensure(tmp_path).cert.read_bytes())
    names = made.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    usage = made.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in usage
    assert not made.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_a_key_others_can_read_is_replaced(tmp_path: Path) -> None:
    first = certificate.ensure(tmp_path)
    first.key.chmod(0o644)
    again = certificate.ensure(tmp_path)
    assert again.fingerprint != first.fingerprint
    assert stat.S_IMODE(again.key.stat().st_mode) == 0o600
