"""Apple App Attest: enrolment (attestation) and per-capture assertions.

Follows Apple's "Validating apps that connect to your server". Any failure raises
`AttestationError`; callers treat the device as unattested.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cbor2
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gateway.capture import der
from gateway.capture.errors import AttestationError

NONCE_EXTENSION_OID = x509.ObjectIdentifier("1.2.840.113635.100.8.2")
AAGUID_PRODUCTION = b"appattest" + b"\x00" * 7
AAGUID_DEVELOPMENT = b"appattestdevelop"


@dataclass(frozen=True)
class AppleDevice:
    key_id: bytes  # SHA-256 of the public key, as the app reports it
    public_key_der: bytes  # SubjectPublicKeyInfo
    counter: int


@dataclass(frozen=True)
class AuthData:
    rp_id_hash: bytes
    counter: int
    aaguid: bytes
    credential_id: bytes


def parse_auth_data(data: bytes, attested: bool) -> AuthData:
    if len(data) < 37:
        raise AttestationError("authenticator data too short")
    rp_id_hash, counter = data[:32], int.from_bytes(data[33:37], "big")
    if not attested:
        return AuthData(rp_id_hash, counter, b"", b"")
    if len(data) < 55:
        raise AttestationError("attested credential data missing")
    aaguid = data[37:53]
    length = int.from_bytes(data[53:55], "big")
    credential_id = data[55 : 55 + length]
    if len(credential_id) != length:
        raise AttestationError("credential id truncated")
    return AuthData(rp_id_hash, counter, aaguid, credential_id)


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


def _nonce_from_extension(cert: x509.Certificate) -> bytes:
    try:
        ext = cert.extensions.get_extension_for_oid(NONCE_EXTENSION_OID)
    except x509.ExtensionNotFound as exc:
        raise AttestationError("nonce extension missing") from exc
    value = ext.value
    raw = value.value if isinstance(value, x509.UnrecognizedExtension) else b""
    try:
        # SEQUENCE { [1] EXPLICIT OCTET STRING nonce }
        tagged = der.parse(raw).expect(der.UNIVERSAL, der.SEQUENCE).children()[0]
        return (
            tagged.expect(der.CONTEXT, 1)
            .children()[0]
            .expect(der.UNIVERSAL, der.OCTET_STRING)
            .value
        )
    except (der.DerError, IndexError) as exc:
        raise AttestationError("malformed nonce extension") from exc


def verify_attestation(
    *,
    attestation_object: bytes,
    key_id: bytes,
    challenge: bytes,
    app_id: str,
    production: bool,
    roots: list[x509.Certificate],
    now: datetime,
) -> AppleDevice:
    """Enrolment: proves `key_id` is a Secure Enclave key in our genuine app."""
    try:
        obj: Any = cbor2.loads(attestation_object)
        statement, auth_data_raw = obj["attStmt"], obj["authData"]
        x5c = statement["x5c"]
        if obj["fmt"] != "apple-appattest" or not isinstance(auth_data_raw, bytes):
            raise AttestationError("not an App Attest attestation")
        chain = [x509.load_der_x509_certificate(c) for c in x5c]
    except AttestationError:
        raise
    except (cbor2.CBORDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AttestationError("malformed attestation object") from exc

    verify_chain(chain, roots, now)
    credential = chain[0]

    client_data_hash = hashlib.sha256(challenge).digest()
    nonce = hashlib.sha256(auth_data_raw + client_data_hash).digest()
    if not hmac.compare_digest(_nonce_from_extension(credential), nonce):
        raise AttestationError("nonce does not match the challenge")

    public_key = credential.public_key()
    if (
        not isinstance(public_key, ec.EllipticCurvePublicKey)
        or public_key.curve.name != "secp256r1"
    ):
        raise AttestationError("unexpected key type")
    point = public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    if not hmac.compare_digest(hashlib.sha256(point).digest(), key_id):
        raise AttestationError("key id does not match the attested key")

    auth = parse_auth_data(auth_data_raw, attested=True)
    if not hmac.compare_digest(auth.rp_id_hash, hashlib.sha256(app_id.encode()).digest()):
        raise AttestationError("attestation is for another app")
    if auth.counter != 0:
        raise AttestationError("counter must start at 0")
    if auth.aaguid != (AAGUID_PRODUCTION if production else AAGUID_DEVELOPMENT):
        raise AttestationError("wrong App Attest environment")
    if auth.credential_id != key_id:
        raise AttestationError("credential id does not match the key id")

    return AppleDevice(
        key_id=key_id,
        public_key_der=public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
        counter=0,
    )


def verify_assertion(
    *,
    assertion: bytes,
    client_data: bytes,
    public_key_der: bytes,
    stored_counter: int,
    app_id: str,
) -> int:
    """Per capture: the enrolled key signed `client_data`. Returns the new counter, which must
    be stored so a captured assertion cannot be replayed."""
    try:
        obj: Any = cbor2.loads(assertion)
        signature, auth_data_raw = obj["signature"], obj["authenticatorData"]
        if not isinstance(signature, bytes) or not isinstance(auth_data_raw, bytes):
            raise AttestationError("malformed assertion")
        public_key = serialization.load_der_public_key(public_key_der)
    except AttestationError:
        raise
    except (cbor2.CBORDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AttestationError("malformed assertion") from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise AttestationError("stored key is not an EC key")

    nonce = hashlib.sha256(auth_data_raw + hashlib.sha256(client_data).digest()).digest()
    try:
        public_key.verify(signature, nonce, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise AttestationError("assertion signature invalid") from exc

    auth = parse_auth_data(auth_data_raw, attested=False)
    if not hmac.compare_digest(auth.rp_id_hash, hashlib.sha256(app_id.encode()).digest()):
        raise AttestationError("assertion is for another app")
    if auth.counter <= stored_counter:
        raise AttestationError("assertion counter did not increase")
    return auth.counter
