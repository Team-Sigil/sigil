import hashlib
import json
from typing import Any

import base58
from cryptography.fernet import Fernet
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

ED25519_KEY_BYTES = 32
ED25519_SIGNATURE_BYTES = 64


def canonical_json(obj: Any) -> str:
    """Canonical JSON for hashing and signing (SPEC §5.4)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def decode_pubkey(pubkey_b58: str) -> bytes | None:
    """Returns the 32 raw key bytes, or None if the string is not a valid Solana public key."""
    try:
        raw = base58.b58decode(pubkey_b58)
    except ValueError:
        return None
    return raw if len(raw) == ED25519_KEY_BYTES else None


def is_valid_pubkey(pubkey_b58: str) -> bool:
    return decode_pubkey(pubkey_b58) is not None


def verify_ed25519(pubkey_b58: str, message: str, signature_b58: str) -> bool:
    """Verifies a Phantom `signMessage` signature over the UTF-8 message; malformed is False."""
    key = decode_pubkey(pubkey_b58)
    if key is None:
        return False
    try:
        signature = base58.b58decode(signature_b58)
    except ValueError:
        return False
    if len(signature) != ED25519_SIGNATURE_BYTES:
        return False
    try:
        VerifyKey(key).verify(message.encode("utf-8"), signature)
    except BadSignatureError:
        return False
    return True


def encrypt(key: str, plaintext: bytes) -> bytes:
    """Fernet encryption for chats and proofs at rest (SEC-15)."""
    return Fernet(key.encode()).encrypt(plaintext)


def decrypt(key: str, token: bytes) -> bytes:
    return Fernet(key.encode()).decrypt(token)
