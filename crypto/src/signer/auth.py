"""Request authentication (SEC-03): every gateway request carries `X-Timestamp`, `X-Nonce`
and `X-Signature` = hex HMAC-SHA256(secret, `{timestamp}.{nonce}.{raw_body}`).
"""

import hashlib
import hmac

from signer.store import SignerStore

MAX_CLOCK_SKEW_SECONDS = 30
MAX_NONCE_CHARS = 64


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = f"{timestamp}.{nonce}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_request(
    store: SignerStore,
    secret: str,
    timestamp: str,
    nonce: str,
    signature: str,
    body: bytes,
    now: float,
) -> bool:
    """True only for a fresh, correctly signed request with an unseen nonce. The nonce is
    remembered only after the signature checks out, so forged requests can't burn nonces."""
    if not timestamp.isdigit() or abs(now - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
        return False
    if not nonce or len(nonce) > MAX_NONCE_CHARS:
        return False
    if not hmac.compare_digest(signature.encode(), sign(secret, timestamp, nonce, body).encode()):
        return False
    return store.remember_nonce(nonce, now)
