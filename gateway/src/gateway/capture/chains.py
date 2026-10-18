"""Certificate chain checks shared by Apple and Android attestation."""

from datetime import datetime

from cryptography import x509
from cryptography.exceptions import InvalidSignature

from gateway.capture.errors import AttestationError


def verify_chain(
    chain: list[x509.Certificate], roots: list[x509.Certificate], now: datetime
) -> None:
    """Each certificate is signed by the next, the last by a trusted root; all are valid now."""
    if not chain:
        raise AttestationError("empty certificate chain")
    for cert, issuer in zip(chain, [*chain[1:], None], strict=True):
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            raise AttestationError("certificate outside its validity period")
        if issuer is None:
            if not any(_signed_by(cert, root) for root in roots):
                raise AttestationError("chain does not end at a trusted root")
        elif not _signed_by(cert, issuer):
            raise AttestationError("broken certificate chain")


def _signed_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True
