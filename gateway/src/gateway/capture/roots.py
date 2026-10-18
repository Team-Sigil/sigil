"""Vendor trust roots shipped with the package (see roots/README.md)."""

from functools import cache
from pathlib import Path

from cryptography import x509

ROOTS_DIR = Path(__file__).parent / "roots"


@cache
def apple_roots() -> list[x509.Certificate]:
    return [_load("apple_app_attestation_root_ca.pem")]


@cache
def google_roots() -> list[x509.Certificate]:
    return [_load(p.name) for p in sorted(ROOTS_DIR.glob("google_*.pem"))]


def _load(name: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate((ROOTS_DIR / name).read_bytes())
