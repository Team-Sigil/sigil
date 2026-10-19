"""The signature on a PDF receipt (SPEC §8.9).

Each test breaks exactly one thing, because every one of these is the difference between a
receipt from the shop and a file somebody made.
"""

from datetime import UTC, datetime, timedelta

import pytest

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
