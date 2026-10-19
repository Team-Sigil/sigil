"""The signature on a PDF receipt (SPEC §8.9).

Each test breaks exactly one thing, because every one of these is the difference between a
receipt from the shop and a file somebody made.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from gateway.proof_pdf import PdfSignatureError, verify
from tests import signed_pdf

ROOT = signed_pdf.root()
LAZADA = signed_pdf.signer(ROOT)
SIGNED = signed_pdf.build(LAZADA)


class TestAccepted:
    def test_a_receipt_signed_by_the_shop_verifies(self) -> None:
        result = verify(SIGNED, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])
        assert result.signer == "Lazada"
        assert result.signed_at is not None

    def test_the_shop_name_is_matched_case_insensitively(self) -> None:
        result = verify(SIGNED, trusted_roots=[ROOT.certificate], allowed_signers=["lazada"])
        assert result.signer == "Lazada"


class TestRefused:
    def test_an_unsigned_pdf(self) -> None:
        with pytest.raises(PdfSignatureError, match="no signature"):
            verify(
                signed_pdf.unsigned(),
                trusted_roots=[ROOT.certificate],
                allowed_signers=["Lazada"],
            )

    def test_a_changed_total(self) -> None:
        """The whole point: edit the amount and the digest no longer matches."""
        tampered = SIGNED.replace(b"THB 520.00", b"THB 999.00")
        assert len(tampered) == len(SIGNED)
        with pytest.raises(PdfSignatureError, match="changed after it was signed"):
            verify(tampered, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])

    def test_content_appended_after_signing(self) -> None:
        with pytest.raises(PdfSignatureError, match="added after"):
            verify(
                SIGNED + b"\n% and one more page\n",
                trusted_roots=[ROOT.certificate],
                allowed_signers=["Lazada"],
            )

    def test_a_signature_from_a_different_key(self) -> None:
        """A real signature, but swapped in from another document."""
        other = signed_pdf.build(signed_pdf.signer(signed_pdf.root()), body="Other order")
        start = SIGNED.index(b"/Contents <") + len(b"/Contents <")
        other_start = other.index(b"/Contents <") + len(b"/Contents <")
        length = signed_pdf.SIGNATURE_BYTES * 2
        swapped = (
            SIGNED[:start] + other[other_start : other_start + length] + SIGNED[start + length :]
        )
        with pytest.raises(PdfSignatureError):
            verify(swapped, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])

    def test_a_shop_we_do_not_accept(self) -> None:
        """Correctly signed, by someone who is not on the list."""
        elsewhere = signed_pdf.build(signed_pdf.signer(ROOT, organisation="Some Other Shop"))
        with pytest.raises(PdfSignatureError, match="not an accepted shop"):
            verify(elsewhere, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])

    def test_a_certificate_from_an_untrusted_root(self) -> None:
        """Self-signed by anyone claiming to be Lazada."""
        impostor = signed_pdf.build(signed_pdf.signer(signed_pdf.root("Someone Else")))
        with pytest.raises(PdfSignatureError, match="not issued by a trusted root"):
            verify(impostor, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])

    def test_an_expired_certificate(self) -> None:
        past = datetime.now(UTC) - timedelta(days=10)
        expired = signed_pdf.signer(
            ROOT, not_valid_before=past - timedelta(days=1), not_valid_after=past
        )
        with pytest.raises(PdfSignatureError, match="expired"):
            verify(
                signed_pdf.build(expired),
                trusted_roots=[ROOT.certificate],
                allowed_signers=["Lazada"],
            )

    def test_no_configured_roots_accepts_nothing(self) -> None:
        """The default: until someone configures the shop's certificate, no PDF counts."""
        with pytest.raises(PdfSignatureError, match="no trusted roots"):
            verify(SIGNED, trusted_roots=[], allowed_signers=["Lazada"])

    @pytest.mark.parametrize(
        "pdf",
        [
            b"%PDF-1.7\n/ByteRange [0 10 20 30] /Contents <zz> \n",
            b"%PDF-1.7\n/ByteRange [5 10 20 30] /Contents <00> \n",
            b"%PDF-1.7\n/Contents <00> \n",
        ],
    )
    def test_malformed_signature_fields(self, pdf: bytes) -> None:
        with pytest.raises(PdfSignatureError):
            verify(pdf, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])

    def test_a_gap_that_leaves_out_the_delimiters(self) -> None:
        """Real signers leave the whole `<…>` string out of the byte range (ISO 32000-1
        §12.8.1); a gap of only the hex inside it is not where the signature is."""
        start = SIGNED.index(b"/Contents <") + len(b"/Contents ")
        end = start + signed_pdf.SIGNATURE_BYTES * 2 + 2
        old = SIGNED[SIGNED.index(b"[0 ") :][:45]
        inner = f"[0 {start + 1} {end - 1} {len(SIGNED) - end + 1}]".encode().ljust(45, b" ")
        shifted = SIGNED.replace(old, inner)
        with pytest.raises(PdfSignatureError, match="not where the byte range leaves a gap"):
            verify(shifted, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])

    def test_a_signature_that_is_not_cms(self) -> None:
        start = SIGNED.index(b"/Contents <") + len(b"/Contents <")
        length = signed_pdf.SIGNATURE_BYTES * 2
        rubbish = b"ab" * (length // 2)
        broken = SIGNED[:start] + rubbish + SIGNED[start + length :]
        with pytest.raises(PdfSignatureError, match="could not be parsed"):
            verify(broken, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])


def _key_usage(*, digital_signature: bool, content_commitment: bool = False) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=digital_signature,
        content_commitment=content_commitment,
        key_encipherment=not digital_signature,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _verify_with(issued: signed_pdf.Issued) -> str:
    return verify(
        signed_pdf.build(issued), trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"]
    ).signer


class TestSignerName:
    @pytest.mark.parametrize(
        "organisation", ["Not Lazada Ltd", "Lazada-Fake Co", "lazada receipts imitation"]
    )
    def test_a_name_that_only_contains_the_shop_is_refused(self, organisation: str) -> None:
        """Anyone the trusted CA certifies with "Lazada" somewhere in their name."""
        with pytest.raises(PdfSignatureError, match="not an accepted shop"):
            _verify_with(signed_pdf.signer(ROOT, organisation=organisation))

    def test_the_exact_name_still_matches_whatever_the_case(self) -> None:
        assert _verify_with(signed_pdf.signer(ROOT, organisation="  LAZADA ")) == "  LAZADA "

    def test_an_empty_allowlist_entry_matches_nothing(self) -> None:
        with pytest.raises(PdfSignatureError, match="not an accepted shop"):
            verify(SIGNED, trusted_roots=[ROOT.certificate], allowed_signers=["", " "])


class TestCertificatePurpose:
    def test_a_tls_certificate_cannot_sign_a_receipt(self) -> None:
        tls = signed_pdf.signer(
            ROOT, extensions=(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),)
        )
        with pytest.raises(PdfSignatureError, match="isn't meant for signing documents"):
            _verify_with(tls)

    def test_a_key_that_may_not_sign_is_refused(self) -> None:
        encipher_only = signed_pdf.signer(ROOT, extensions=(_key_usage(digital_signature=False),))
        with pytest.raises(PdfSignatureError, match="key usage doesn't allow signatures"):
            _verify_with(encipher_only)

    @pytest.mark.parametrize(
        "extensions",
        [
            (_key_usage(digital_signature=True),),
            (_key_usage(digital_signature=False, content_commitment=True),),
            (x509.ExtendedKeyUsage([x509.ObjectIdentifier("1.3.6.1.5.5.7.3.36")]),),
            (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]),),
            (
                _key_usage(digital_signature=True),
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH, x509.ObjectIdentifier("1.2.840.113583.1.1.5")]
                ),
            ),
        ],
    )
    def test_a_document_signing_certificate_is_accepted(
        self, extensions: tuple[x509.ExtensionType, ...]
    ) -> None:
        assert _verify_with(signed_pdf.signer(ROOT, extensions=extensions)) == "Lazada"


class TestRealSignerEncodings:
    def test_a_ber_signature_ending_in_zero_bytes_verifies(self) -> None:
        """Lazada's real receipts: the signature uses BER indefinite lengths, so its last
        bytes are 00 00 end-of-content markers. Stripping trailing zeros as padding broke
        every one of them ("the signature could not be parsed")."""
        pdf = signed_pdf.build(LAZADA, indefinite=True)
        assert verify(pdf, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"]).signer == (
            "Lazada"
        )

    def test_the_real_signers_full_company_name_matches_exactly(self) -> None:
        """What a real Lazada Thailand receipt carries as its organisation."""
        thai = signed_pdf.signer(ROOT, organisation="LAZADA EXPRESS COMPANY LIMITED")
        pdf = signed_pdf.build(thai, indefinite=True)
        name = verify(
            pdf,
            trusted_roots=[ROOT.certificate],
            allowed_signers=["Lazada Express Company Limited"],
        ).signer
        assert name == "LAZADA EXPRESS COMPANY LIMITED"
        with pytest.raises(PdfSignatureError, match="not an accepted shop"):
            verify(pdf, trusted_roots=[ROOT.certificate], allowed_signers=["Lazada"])
