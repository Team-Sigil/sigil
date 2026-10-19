"""Building signed PDFs for tests.

Makes the smallest thing that is still a real signature: a PDF whose `/ByteRange` covers
everything but the `/Contents` hole, with a genuine CMS SignedData in that hole, signed by a
throwaway certificate chain. Tests can then break one piece at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from asn1crypto import algos, cms  # type: ignore[import-untyped]
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

SIGNATURE_BYTES = 4096  # the hole reserved for the CMS blob, as a signer would reserve it


@dataclass
class Issued:
    certificate: x509.Certificate
    key: rsa.RSAPrivateKey


def _name(common: str, organisation: str | None = None) -> x509.Name:
    parts = [x509.NameAttribute(NameOID.COMMON_NAME, common)]
    if organisation:
        parts.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organisation))
    return x509.Name(parts)


def root(common: str = "Test Receipt Root") -> Issued:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common))
        .issuer_name(_name(common))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return Issued(certificate, key)


def signer(
    issuer: Issued,
    *,
    organisation: str = "Lazada",
    common: str = "Lazada Order Receipts",
    not_valid_before: datetime | None = None,
    not_valid_after: datetime | None = None,
) -> Issued:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common, organisation))
        .issuer_name(issuer.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before or now - timedelta(days=1))
        .not_valid_after(not_valid_after or now + timedelta(days=365))
        .sign(issuer.key, hashes.SHA256())
    )
    return Issued(certificate, key)


def _cms(signed_bytes: bytes, issued: Issued) -> bytes:
    """A CMS SignedData over the document digest, as a PDF signer produces."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(signed_bytes)
    message_digest = digest.finalize()

    attributes = cms.CMSAttributes(
        [
            cms.CMSAttribute({"type": "content_type", "values": [cms.ContentType("data")]}),
            cms.CMSAttribute(
                {
                    "type": "signing_time",
                    "values": [cms.Time({"utc_time": datetime.now(UTC)})],
                }
            ),
            cms.CMSAttribute(
                {"type": "message_digest", "values": [cms.OctetString(message_digest)]}
            ),
        ]
    )
    signature = issued.key.sign(attributes.dump(), padding.PKCS1v15(), hashes.SHA256())
    certificate = asn1_x509.Certificate.load(
        issued.certificate.public_bytes(serialization.Encoding.DER)
    )
    signer_info = cms.SignerInfo(
        {
            "version": "v1",
            "sid": cms.SignerIdentifier(
                {
                    "issuer_and_serial_number": cms.IssuerAndSerialNumber(
                        {
                            "issuer": certificate.issuer,
                            "serial_number": certificate.serial_number,
                        }
                    )
                }
            ),
            "digest_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
            "signed_attrs": attributes,
            "signature_algorithm": algos.SignedDigestAlgorithm({"algorithm": "rsassa_pkcs1v15"}),
            "signature": signature,
        }
    )
    signed_data = cms.SignedData(
        {
            "version": "v1",
            "digest_algorithms": [algos.DigestAlgorithm({"algorithm": "sha256"})],
            "encap_content_info": {"content_type": "data"},
            "certificates": [certificate],
            "signer_infos": [signer_info],
        }
    )
    der: bytes = cms.ContentInfo({"content_type": "signed_data", "content": signed_data}).dump()
    return der


def build(issued: Issued, *, body: str = "Lazada order 12345  THB 520.00") -> bytes:
    """A signed PDF. The body is plain text in the file, which is all these tests need."""
    head = b"%PDF-1.7\n% a receipt\n1 0 obj\n<< /Body (" + body.encode() + b") >>\nendobj\n"
    placeholder = b"0" * (SIGNATURE_BYTES * 2)

    # Lay the file out once with a dummy range to learn the offsets, then write it properly.
    def assemble(byte_range: bytes, contents: bytes) -> bytes:
        return (
            head
            + b"2 0 obj\n<< /Type /Sig /ByteRange "
            + byte_range
            + b" /Contents <"
            + contents
            + b"> >>\nendobj\n%%EOF\n"
        )

    # The hole is the whole /Contents string, `<` and `>` included, as real signers leave it
    # (ISO 32000-1 §12.8.1).
    def hole(laid_out: bytes) -> tuple[int, int]:
        start = laid_out.index(b"/Contents <") + len(b"/Contents ")
        return start, start + len(placeholder) + 2

    template = assemble(b"[0000000000 0000000000 0000000000 0000000000]", placeholder)
    hole_start, hole_end = hole(template)
    byte_range = f"[0 {hole_start} {hole_end} {len(template) - hole_end}]".encode().ljust(45, b" ")
    # Keep the length identical so the offsets computed above still hold.
    byte_range = byte_range[:45]
    laid_out = assemble(byte_range, placeholder)
    hole_start, hole_end = hole(laid_out)
    byte_range = f"[0 {hole_start} {hole_end} {len(laid_out) - hole_end}]".encode().ljust(45, b" ")[
        :45
    ]
    laid_out = assemble(byte_range, placeholder)

    signed_bytes = laid_out[:hole_start] + laid_out[hole_end:]
    blob = _cms(signed_bytes, issued)
    assert len(blob) <= SIGNATURE_BYTES, "reserve more room for the signature"
    contents = blob.hex().encode().ljust(SIGNATURE_BYTES * 2, b"0")
    return assemble(byte_range, contents)


def unsigned(body: str = "Lazada order 12345  THB 520.00") -> bytes:
    return b"%PDF-1.7\n% a receipt\n1 0 obj\n<< /Body (" + body.encode() + b") >>\nendobj\n%%EOF\n"


def roots_pem(*certificates: x509.Certificate) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certificates)


__all__ = ["Issued", "build", "root", "roots_pem", "signer", "unsigned"]
