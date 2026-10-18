"""Which apps and vendors the gateway trusts for verified capture."""

from dataclasses import dataclass, field

from cryptography import x509

from gateway.capture.play_integrity import (
    GooglePlayIntegrity,
    IntegrityVerifier,
    NoIntegrityVerifier,
)
from gateway.capture.roots import apple_roots, google_roots
from gateway.core.config import Settings


@dataclass
class CaptureConfig:
    apple_app_id: str | None
    apple_production: bool
    android_package: str | None
    android_signing_digests: set[bytes]
    integrity: IntegrityVerifier = field(default_factory=NoIntegrityVerifier)
    apple_roots: list[x509.Certificate] = field(default_factory=apple_roots)
    google_roots: list[x509.Certificate] = field(default_factory=google_roots)


def capture_config(settings: Settings) -> CaptureConfig:
    digests = {
        bytes.fromhex(d.strip().replace(":", ""))
        for d in settings.android_signing_cert_sha256.split(",")
        if d.strip()
    }
    integrity: IntegrityVerifier = NoIntegrityVerifier()
    if settings.android_package and settings.play_integrity_service_account_file:
        integrity = GooglePlayIntegrity.from_file(
            settings.android_package, settings.play_integrity_service_account_file
        )
    return CaptureConfig(
        apple_app_id=settings.apple_app_id,
        apple_production=settings.apple_app_attest_production,
        android_package=settings.android_package,
        android_signing_digests=digests,
        integrity=integrity,
    )
