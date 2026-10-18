from datetime import timedelta
from typing import Any

import pytest

from gateway.capture import apple, der
from gateway.capture.errors import AttestationError
from gateway.capture.roots import apple_roots
from tests.capture_fakes import APP_ID, NOW, AppleDevice, make_ca

CHALLENGE = b"enrolment-challenge-0123456789"


def enrol(device: AppleDevice, attestation: bytes, **overrides: Any) -> apple.AppleDevice:
    kwargs: dict[str, Any] = {
        "attestation_object": attestation,
        "key_id": device.key_id,
        "challenge": CHALLENGE,
        "app_id": APP_ID,
        "production": True,
        "roots": [device.root.cert],
        "now": NOW,
    }
    kwargs.update(overrides)
    return apple.verify_attestation(**kwargs)


class TestAttestation:
    def test_genuine_attestation_enrols_the_key(self) -> None:
        device = AppleDevice()
        enrolled = enrol(device, device.attest(CHALLENGE))
        assert enrolled.key_id == device.key_id and enrolled.counter == 0

    def test_chain_from_another_root_is_refused(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="trusted root"):
            enrol(device, device.attest(CHALLENGE), roots=[make_ca("Not Apple").cert])

    def test_real_apple_root_does_not_trust_our_test_chain(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="trusted root"):
            enrol(device, device.attest(CHALLENGE), roots=apple_roots())

    def test_replayed_attestation_for_another_challenge_is_refused(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="nonce"):
            enrol(device, device.attest(CHALLENGE), challenge=b"a different challenge")

    def test_forged_nonce_is_refused(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="nonce"):
            enrol(device, device.attest(CHALLENGE, nonce_override=b"\x00" * 32))

    def test_key_id_must_be_the_attested_key(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="key id"):
            enrol(device, device.attest(CHALLENGE), key_id=AppleDevice().key_id)

    def test_attestation_for_another_app_is_refused(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="another app"):
            enrol(device, device.attest(CHALLENGE, app_id="OTHER.com.evil"))

    def test_development_key_is_refused_in_production(self) -> None:
        device = AppleDevice()
        attestation = device.attest(CHALLENGE, aaguid=b"appattestdevelop")
        with pytest.raises(AttestationError, match="environment"):
            enrol(device, attestation)
        assert enrol(device, attestation, production=False).counter == 0

    def test_counter_must_start_at_zero(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="counter"):
            enrol(device, device.attest(CHALLENGE, counter=1))

    def test_expired_certificate_is_refused(self) -> None:
        device = AppleDevice()
        with pytest.raises(AttestationError, match="validity"):
            enrol(device, device.attest(CHALLENGE), now=NOW + timedelta(days=60))

    @pytest.mark.parametrize("garbage", [b"", b"\xff\x00", b"not cbor at all"])
    def test_garbage_is_refused(self, garbage: bytes) -> None:
        with pytest.raises(AttestationError):
            enrol(AppleDevice(), garbage)


class TestAssertion:
    def enrolled(self) -> tuple[AppleDevice, apple.AppleDevice]:
        device = AppleDevice()
        return device, enrol(device, device.attest(CHALLENGE))

    def test_signed_claim_verifies_and_advances_the_counter(self) -> None:
        device, stored = self.enrolled()
        counter = apple.verify_assertion(
            assertion=device.assert_(b"claim"),
            client_data=b"claim",
            public_key_der=stored.public_key_der,
            stored_counter=stored.counter,
            app_id=APP_ID,
        )
        assert counter == 1

    def test_tampered_claim_is_refused(self) -> None:
        device, stored = self.enrolled()
        with pytest.raises(AttestationError, match="signature"):
            apple.verify_assertion(
                assertion=device.assert_(b"claim"),
                client_data=b"claim with a different hash",
                public_key_der=stored.public_key_der,
                stored_counter=0,
                app_id=APP_ID,
            )

    def test_replayed_assertion_is_refused(self) -> None:
        device, stored = self.enrolled()
        assertion = device.assert_(b"claim")
        with pytest.raises(AttestationError, match="counter"):
            apple.verify_assertion(
                assertion=assertion,
                client_data=b"claim",
                public_key_der=stored.public_key_der,
                stored_counter=1,
                app_id=APP_ID,
            )

    def test_another_devices_signature_is_refused(self) -> None:
        _, stored = self.enrolled()
        with pytest.raises(AttestationError, match="signature"):
            apple.verify_assertion(
                assertion=AppleDevice().assert_(b"claim"),
                client_data=b"claim",
                public_key_der=stored.public_key_der,
                stored_counter=0,
                app_id=APP_ID,
            )


def test_der_reader_handles_high_tag_numbers_and_rejects_truncation() -> None:
    from tests.capture_fakes import explicit, integer

    item = der.parse(explicit(709, integer(5)))
    assert (item.cls, item.number) == (der.CONTEXT, 709)
    assert item.children()[0].as_int() == 5
    with pytest.raises(der.DerError):
        der.parse(explicit(709, integer(5))[:-1])
    with pytest.raises(der.DerError):
        der.parse(integer(5) + b"\x00")
