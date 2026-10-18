"""Which iOS app the gateway trusts for verified capture."""

from dataclasses import dataclass, field

from cryptography import x509

from gateway.capture.roots import apple_roots
from gateway.core.config import Settings


@dataclass
class CaptureConfig:
    apple_app_id: str | None  # "<Team ID>.<bundle id>"; None: capture is off
    apple_production: bool
    apple_roots: list[x509.Certificate] = field(default_factory=apple_roots)


def capture_config(settings: Settings) -> CaptureConfig:
    return CaptureConfig(
        apple_app_id=settings.apple_app_id or None,
        apple_production=settings.apple_app_attest_production,
    )
