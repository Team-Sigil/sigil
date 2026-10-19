"""Verifying the digital signature on a PDF receipt.

An online order cannot be photographed, so its receipt arrives as the PDF the shop sent.
A photo is tied to reality by App Attest; a PDF is tied to reality only by the shop's own
signature over it. This module answers one question: **was this file signed by a shop we
trust, and has it changed since?**

What a good answer proves:

- the bytes have not changed since they were signed (the signed digest covers them);
- the signing certificate chains to a root we were configured to trust;
- the certificate belongs to an issuer on the allowlist (Lazada today).

What it does **not** prove: that the order is this dependent's, that it is for the amount
asked for, or that it was not forwarded by someone else. The agent still reads the merchant,
date and total, and the engine still checks that the findings cover the transfer.

Only a PDF that passes here is shown to the model as evidence (SPEC §8.9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from asn1crypto import cms  # type: ignore[import-untyped]
from asn1crypto import x509 as asn1_x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509 import Certificate, load_der_x509_certificate
from cryptography.x509.oid import NameOID

# A signature dictionary names the ranges it covers and carries the CMS blob between them.
BYTE_RANGE = re.compile(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]")
CONTENTS = re.compile(rb"/Contents\s*<([0-9A-Fa-f\s]+)>")

DIGESTS: dict[str, hashes.HashAlgorithm] = {
    "sha256": hashes.SHA256(),
    "sha384": hashes.SHA384(),
    "sha512": hashes.SHA512(),
}


class PdfSignatureError(Exception):
    """The PDF is not signed in a way we can accept. The message is for our logs and the
    `reason` we store, never for the model."""


@dataclass(frozen=True)
class SignedPdf:
    """A PDF whose signature verified."""

    signer: str  # the certificate's organisation, or its common name
    signed_at: datetime | None  # the claimed signing time, if the signature carries one


def verify(
    pdf: bytes, *, trusted_roots: list[Certificate], allowed_signers: list[str]
) -> SignedPdf:
    """Verify an embedded PDF signature, or raise `PdfSignatureError`.

    `allowed_signers` are matched case-insensitively against the signing certificate's
    organisation and common name, so a receipt signed by someone else is refused even when
    its certificate is otherwise valid.
    """
    signed_bytes, signature_der = _extract(pdf)
    signed_data = _signed_data(signature_der)
    signer_info = _only_signer(signed_data)
    certificate = _signer_certificate(signed_data, signer_info)

    _check_digest(signer_info, signed_bytes)
    _check_signature(signer_info, certificate)
    _check_validity(certificate)
    _check_chain(certificate, trusted_roots)
    name = _signer_name(certificate)
    if not _allowed(name, allowed_signers):
        raise PdfSignatureError(f"signed by {name!r}, which is not an accepted shop")
    return SignedPdf(signer=name, signed_at=_signing_time(signer_info))


def _extract(pdf: bytes) -> tuple[bytes, bytes]:
    """The bytes the signature covers, and the signature itself.

    The `/ByteRange` must cover the whole file apart from the hole holding the signature.
    Anything outside it — an appended page, a changed total — would be unsigned content in a
    file that looks signed, so a mismatch is refused rather than ignored.
    """
    ranges = BYTE_RANGE.search(pdf)
    contents = CONTENTS.search(pdf)
    if ranges is None or contents is None:
        raise PdfSignatureError("no signature in the document")

    start_a, length_a, start_b, length_b = (int(value) for value in ranges.groups())
    hole_start, hole_end = start_a + length_a, start_b
    if start_a != 0 or hole_start >= hole_end or start_b + length_b > len(pdf):
        raise PdfSignatureError("the signature's byte range is not a valid span of the file")
    if start_b + length_b != len(pdf):
        raise PdfSignatureError("content was added after the document was signed")

    # The hole left out of the ranges must be exactly the hex of /Contents; otherwise the
    # signature covers bytes other than the ones it appears to.
    if contents.start(1) != hole_start or contents.end(1) > hole_end:
        raise PdfSignatureError("the signature is not where the byte range leaves a gap")

    signed = pdf[start_a : start_a + length_a] + pdf[start_b : start_b + length_b]
    try:
        signature = bytes.fromhex(re.sub(rb"\s", b"", contents.group(1)).decode("ascii"))
    except ValueError as exc:
        raise PdfSignatureError("the signature is not valid hex") from exc
    return signed, signature.rstrip(b"\x00")


def _signed_data(der: bytes) -> cms.SignedData:
    try:
        info = cms.ContentInfo.load(der)
        if info["content_type"].native != "signed_data":
            raise PdfSignatureError("the signature is not a CMS SignedData")
        return info["content"]
    except PdfSignatureError:
        raise
    except Exception as exc:  # asn1crypto raises bare ValueErrors on malformed input
        raise PdfSignatureError("the signature could not be parsed") from exc


def _only_signer(signed_data: cms.SignedData) -> cms.SignerInfo:
    signers = signed_data["signer_infos"]
    if len(signers) != 1:
        raise PdfSignatureError("expected exactly one signer")
    return signers[0]


def _signer_certificate(signed_data: cms.SignedData, signer: cms.SignerInfo) -> Certificate:
    """The certificate the signer identified, taken from the signature's own set."""
    identifier = signer["sid"]
    for choice in signed_data["certificates"]:
        if choice.name != "certificate":
            continue
        candidate: asn1_x509.Certificate = choice.chosen
        if identifier.name == "issuer_and_serial_number":
            same_issuer = candidate.issuer == identifier.chosen["issuer"]
            same_serial = candidate.serial_number == identifier.chosen["serial_number"].native
            if same_issuer and same_serial:
                return load_der_x509_certificate(candidate.dump())
        elif candidate.key_identifier == identifier.chosen.native:
            return load_der_x509_certificate(candidate.dump())
    raise PdfSignatureError("the signing certificate is not in the signature")


def _check_digest(signer: cms.SignerInfo, signed_bytes: bytes) -> None:
    """The signed attributes carry a digest of the document; it must match what we read."""
    attributes = signer["signed_attrs"]
    if not attributes:
        raise PdfSignatureError("the signature has no signed attributes")
    declared = next(
        (
            attr["values"][0].native
            for attr in attributes
            if attr["type"].native == "message_digest"
        ),
        None,
    )
    if declared is None:
        raise PdfSignatureError("the signature does not say what it covers")

    algorithm = _digest_algorithm(signer)
    digest = hashes.Hash(algorithm)
    digest.update(signed_bytes)
    if digest.finalize() != declared:
        raise PdfSignatureError("the document changed after it was signed")


def _check_signature(signer: cms.SignerInfo, certificate: Certificate) -> None:
    """The signature is over the DER of the signed attributes, not the document itself."""
    attributes = signer["signed_attrs"]
    # Re-encode with the SET OF tag the standard requires, not the context tag in the file.
    payload = attributes.untag().dump()
    algorithm = _digest_algorithm(signer)
    signature = signer["signature"].native
    key = certificate.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, payload, padding.PKCS1v15(), algorithm)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, payload, ec.ECDSA(algorithm))
        else:
            raise PdfSignatureError("unsupported signing key")
    except InvalidSignature as exc:
        raise PdfSignatureError("the signature does not match the document") from exc


def _digest_algorithm(signer: cms.SignerInfo) -> hashes.HashAlgorithm:
    name = signer["digest_algorithm"]["algorithm"].native
    algorithm = DIGESTS.get(name)
    if algorithm is None:
        raise PdfSignatureError(f"unsupported digest {name}")
    return algorithm


def _check_validity(certificate: Certificate, now: datetime | None = None) -> None:
    moment = now or datetime.now(UTC)
    if not certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc:
        raise PdfSignatureError("the signing certificate is expired or not yet valid")


def _check_chain(certificate: Certificate, trusted_roots: list[Certificate]) -> None:
    """The signer must be issued by a root we were told to trust.

    One level only: the shop's signing certificate signed directly by a configured root or
    intermediate. Longer chains and revocation are not handled, so trust exactly the
    certificates that actually issue these receipts.
    """
    if not trusted_roots:
        raise PdfSignatureError("no trusted roots are configured")
    algorithm = certificate.signature_hash_algorithm
    if algorithm is None:
        raise PdfSignatureError("the signing certificate has no usable signature algorithm")
    for root in trusted_roots:
        if certificate.issuer != root.subject:
            continue
        key = root.public_key()
        try:
            if isinstance(key, rsa.RSAPublicKey):
                key.verify(
                    certificate.signature,
                    certificate.tbs_certificate_bytes,
                    padding.PKCS1v15(),
                    algorithm,
                )
            elif isinstance(key, ec.EllipticCurvePublicKey):
                key.verify(
                    certificate.signature,
                    certificate.tbs_certificate_bytes,
                    ec.ECDSA(algorithm),
                )
            else:
                continue
            return
        except InvalidSignature:
            continue
    raise PdfSignatureError("the signing certificate is not issued by a trusted root")


def _signer_name(certificate: Certificate) -> str:
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        values = certificate.subject.get_attributes_for_oid(oid)
        if values:
            return str(values[0].value)
    return ""


def _allowed(name: str, allowed_signers: list[str]) -> bool:
    folded = name.casefold()
    return any(allowed.casefold() in folded for allowed in allowed_signers if allowed)


def _signing_time(signer: cms.SignerInfo) -> datetime | None:
    for attr in signer["signed_attrs"] or []:
        if attr["type"].native == "signing_time":
            value = attr["values"][0].native
            return value if isinstance(value, datetime) else None
    return None
