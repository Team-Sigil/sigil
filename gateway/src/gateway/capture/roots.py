"""Apple's App Attestation root, shipped with the package (see roots/README.md)."""

from functools import cache
from pathlib import Path

from cryptography import x509

ROOTS_DIR = Path(__file__).parent / "roots"


@cache
def apple_roots() -> list[x509.Certificate]:
    path = ROOTS_DIR / "apple_app_attestation_root_ca.pem"
    return [x509.load_pem_x509_certificate(path.read_bytes())]
