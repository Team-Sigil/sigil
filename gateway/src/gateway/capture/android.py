"""Android key attestation: proves a signing key lives in secure hardware (TEE or StrongBox),
on a locked device with verified boot, inside our app signed with our release key.

Follows https://developer.android.com/privacy-and-security/security-key-attestation. Any
failure raises `AttestationError`. Play Integrity (`play_integrity.py`) is checked separately.
"""

import hmac
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gateway.capture import der
from gateway.capture.chains import verify_chain
from gateway.capture.errors import AttestationError

KEY_DESCRIPTION_OID = x509.ObjectIdentifier("1.3.6.1.4.1.11129.2.1.17")
SECURITY_LEVELS = {1: "trusted_environment", 2: "strongbox"}  # 0 = software: refused
TAG_ALGORITHM, TAG_ROOT_OF_TRUST, TAG_APPLICATION_ID = 2, 704, 709
ALGORITHM_EC = 3
BOOT_VERIFIED = 0


@dataclass(frozen=True)
class AndroidDevice:
    public_key_der: bytes  # SubjectPublicKeyInfo
    security_level: str


def _authorizations(item: der.Tlv) -> dict[int, der.Tlv]:
    """An AuthorizationList: explicitly tagged optional fields, by tag number."""
    return {
        field.number: field.children()[0]
        for field in item.expect(der.UNIVERSAL, der.SEQUENCE).children()
        if field.cls == der.CONTEXT
    }


def _check_application(application: der.Tlv, package: str, signing_digests: set[bytes]) -> None:
    # attestationApplicationId: an OCTET STRING holding
    # SEQUENCE { SET OF SEQUENCE { packageName, version }, SET OF signatureDigest }
    content = der.parse(application.expect(der.UNIVERSAL, der.OCTET_STRING).value)
    packages, digests = content.expect(der.UNIVERSAL, der.SEQUENCE).children()[:2]
    names = {info.children()[0].value for info in packages.children()}
    if package.encode() not in names:
        raise AttestationError("key belongs to another app")
    if not any(d.value in signing_digests for d in digests.children()):
        raise AttestationError("app is not signed with our release key")


def verify_key_attestation(
    *,
    chain: list[bytes],
    challenge: bytes,
    package: str,
    signing_digests: set[bytes],
    roots: list[x509.Certificate],
    now: datetime,
) -> AndroidDevice:
    try:
        certs = [x509.load_der_x509_certificate(c) for c in chain]
    except ValueError as exc:
        raise AttestationError("malformed certificate") from exc
    verify_chain(certs, roots, now)
    leaf = certs[0]

    try:
        ext = leaf.extensions.get_extension_for_oid(KEY_DESCRIPTION_OID).value
        raw = ext.value if isinstance(ext, x509.UnrecognizedExtension) else b""
        fields = der.parse(raw).expect(der.UNIVERSAL, der.SEQUENCE).children()
        attestation_level = fields[1].expect(der.UNIVERSAL, der.ENUMERATED).as_int()
        keymint_level = fields[3].expect(der.UNIVERSAL, der.ENUMERATED).as_int()
        attested_challenge = fields[4].expect(der.UNIVERSAL, der.OCTET_STRING).value
        software, hardware = _authorizations(fields[6]), _authorizations(fields[7])

        if attestation_level not in SECURITY_LEVELS or keymint_level not in SECURITY_LEVELS:
            raise AttestationError("key is not hardware-backed")
        if not hmac.compare_digest(attested_challenge, challenge):
            raise AttestationError("challenge does not match")

        root_of_trust = hardware.get(TAG_ROOT_OF_TRUST)
        if root_of_trust is None:
            raise AttestationError("no hardware root of trust")
        trust = root_of_trust.expect(der.UNIVERSAL, der.SEQUENCE).children()
        if not trust[1].as_bool() or trust[2].as_int() != BOOT_VERIFIED:
            raise AttestationError("device is unlocked or boot is not verified")

        algorithm = hardware.get(TAG_ALGORITHM)
        if algorithm is None or algorithm.as_int() != ALGORITHM_EC:
            raise AttestationError("key is not an EC key")

        application = software.get(TAG_APPLICATION_ID) or hardware.get(TAG_APPLICATION_ID)
        if application is None:
            raise AttestationError("no application id")
        _check_application(application, package, signing_digests)
    except x509.ExtensionNotFound as exc:
        raise AttestationError("key attestation extension missing") from exc
    except (der.DerError, IndexError) as exc:
        raise AttestationError("malformed key description") from exc

    public_key = leaf.public_key()
    if (
        not isinstance(public_key, ec.EllipticCurvePublicKey)
        or public_key.curve.name != "secp256r1"
    ):
        raise AttestationError("unexpected key type")
    return AndroidDevice(
        public_key_der=public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
        security_level=SECURITY_LEVELS[attestation_level],
    )
