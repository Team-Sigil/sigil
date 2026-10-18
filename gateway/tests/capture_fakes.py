"""Synthetic attestations for tests: our own CAs standing in for Apple's and Google's, with
devices that produce App Attest attestations and assertions or Android key-attestation chains.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
APP_ID = "TEAMID1234.com.sigil.app"
PACKAGE = "com.sigil.app"
SIGNING_DIGEST = hashlib.sha256(b"sigil release signing certificate").digest()


# --- DER encoding (just enough to build the extensions) ---------------------------------------


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def tlv(tag: bytes, content: bytes) -> bytes:
    return tag + _length(len(content)) + content


def tag(cls: int, constructed: bool, number: int) -> bytes:
    first = (cls << 6) | (0x20 if constructed else 0)
    if number < 31:
        return bytes([first | number])
    parts: list[int] = []
    while True:
        parts.insert(0, number & 0x7F)
        number >>= 7
        if not number:
            break
    return bytes([first | 0x1F]) + bytes([p | 0x80 for p in parts[:-1]] + [parts[-1]])


def seq(*items: bytes) -> bytes:
    return tlv(b"\x30", b"".join(items))


def set_of(*items: bytes) -> bytes:
    return tlv(b"\x31", b"".join(items))


def integer(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)
    return tlv(b"\x02", raw)


def enumerated(value: int) -> bytes:
    return tlv(b"\x0a", bytes([value]))


def octets(value: bytes) -> bytes:
    return tlv(b"\x04", value)


def boolean(value: bool) -> bytes:
    return tlv(b"\x01", b"\xff" if value else b"\x00")


def explicit(number: int, content: bytes) -> bytes:
    return tlv(tag(2, True, number), content)


# --- certificates -----------------------------------------------------------------------------


@dataclass
class Ca:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn: str, issuer: Ca | None = None) -> Ca:
    key = ec.generate_private_key(ec.SECP384R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(issuer.cert.subject if issuer else _name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=365))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    )
    return Ca(key, builder.sign(issuer.key if issuer else key, hashes.SHA384()))


def make_leaf(
    issuer: Ca,
    public_key: ec.EllipticCurvePublicKey,
    extension_oid: str,
    extension_value: bytes,
    not_after: datetime = NOW + timedelta(days=30),
) -> x509.Certificate:
    return (
        x509.CertificateBuilder()
        .subject_name(_name("device"))
        .issuer_name(issuer.cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(not_after)
        .add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(extension_oid), extension_value),
            critical=False,
        )
        .sign(issuer.key, hashes.SHA256())
    )


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


# --- Apple App Attest -------------------------------------------------------------------------


@dataclass
class AppleDevice:
    """An iPhone with a Secure Enclave key, attested by `root` (our stand-in for Apple)."""

    root: Ca = field(default_factory=lambda: make_ca("Test App Attest Root"))
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    counter: int = 0

    @property
    def key_id(self) -> bytes:
        point = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return hashlib.sha256(point).digest()

    def attest(
        self,
        challenge: bytes,
        *,
        app_id: str = APP_ID,
        aaguid: bytes = b"appattest" + b"\x00" * 7,
        counter: int = 0,
        nonce_override: bytes | None = None,
    ) -> bytes:
        intermediate = make_ca("Test App Attest CA 1", self.root)
        auth_data = (
            hashlib.sha256(app_id.encode()).digest()
            + b"\x40"
            + counter.to_bytes(4, "big")
            + aaguid
            + len(self.key_id).to_bytes(2, "big")
            + self.key_id
            + b"cose-key-placeholder"
        )
        nonce = hashlib.sha256(auth_data + hashlib.sha256(challenge).digest()).digest()
        extension = seq(explicit(1, octets(nonce_override or nonce)))
        leaf = make_leaf(intermediate, self.key.public_key(), "1.2.840.113635.100.8.2", extension)
        return cbor2.dumps(
            {
                "fmt": "apple-appattest",
                "attStmt": {"x5c": [der(leaf), der(intermediate.cert)], "receipt": b"receipt"},
                "authData": auth_data,
            }
        )

    def assert_(
        self, client_data: bytes, *, app_id: str = APP_ID, counter: int | None = None
    ) -> bytes:
        self.counter = self.counter + 1 if counter is None else counter
        auth_data = (
            hashlib.sha256(app_id.encode()).digest() + b"\x01" + self.counter.to_bytes(4, "big")
        )
        nonce = hashlib.sha256(auth_data + hashlib.sha256(client_data).digest()).digest()
        signature = self.key.sign(nonce, ec.ECDSA(hashes.SHA256()))
        return cbor2.dumps({"signature": signature, "authenticatorData": auth_data})


# --- Android key attestation ------------------------------------------------------------------


@dataclass
class AndroidDevice:
    """A phone with a hardware-backed key, attested by `root` (our stand-in for Google)."""

    root: Ca = field(default_factory=lambda: make_ca("Test Key Attestation Root"))
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def chain(
        self,
        challenge: bytes,
        *,
        security_level: int = 1,  # TrustedEnvironment
        package: str = PACKAGE,
        signing_digest: bytes = SIGNING_DIGEST,
        device_locked: bool = True,
        boot_state: int = 0,  # Verified
        root_of_trust: bool = True,
    ) -> list[bytes]:
        intermediate = make_ca("Test Attestation Intermediate", self.root)
        application_id = seq(
            set_of(seq(octets(package.encode()), integer(1))),
            set_of(octets(signing_digest)),
        )
        software = seq(explicit(709, octets(application_id)))
        hardware_items = [explicit(2, integer(3))]  # algorithm: EC
        if root_of_trust:
            hardware_items.append(
                explicit(
                    704, seq(octets(b"\x00" * 32), boolean(device_locked), enumerated(boot_state))
                )
            )
        description = seq(
            integer(200),
            enumerated(security_level),
            integer(200),
            enumerated(security_level),
            octets(challenge),
            octets(b""),
            software,
            seq(*hardware_items),
        )
        leaf = make_leaf(
            intermediate, self.key.public_key(), "1.3.6.1.4.1.11129.2.1.17", description
        )
        return [der(leaf), der(intermediate.cert), der(self.root.cert)]

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data, ec.ECDSA(hashes.SHA256()))
