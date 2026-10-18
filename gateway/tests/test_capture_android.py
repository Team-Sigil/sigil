import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.capture import android
from gateway.capture.errors import AttestationError
from gateway.capture.play_integrity import GooglePlayIntegrity, NoIntegrityVerifier, check_verdict
from gateway.capture.roots import google_roots
from tests.capture_fakes import NOW, PACKAGE, SIGNING_DIGEST, AndroidDevice, make_ca

CHALLENGE = b"enrolment-challenge-0123456789"


def verify(device: AndroidDevice, chain: list[bytes], **overrides: Any) -> android.AndroidDevice:
    kwargs: dict[str, Any] = {
        "chain": chain,
        "challenge": CHALLENGE,
        "package": PACKAGE,
        "signing_digests": {SIGNING_DIGEST},
        "roots": [device.root.cert],
        "now": NOW,
    }
    kwargs.update(overrides)
    return android.verify_key_attestation(**kwargs)


class TestKeyAttestation:
    def test_hardware_key_in_our_app_is_accepted(self) -> None:
        device = AndroidDevice()
        enrolled = verify(device, device.chain(CHALLENGE))
        assert enrolled.security_level == "trusted_environment"
        assert (
            verify(device, device.chain(CHALLENGE, security_level=2)).security_level == "strongbox"
        )

    def test_software_key_is_refused(self) -> None:
        device = AndroidDevice()
        with pytest.raises(AttestationError, match="hardware-backed"):
            verify(device, device.chain(CHALLENGE, security_level=0))

    def test_chain_from_another_root_is_refused(self) -> None:
        device = AndroidDevice()
        with pytest.raises(AttestationError, match="trusted root"):
            verify(device, device.chain(CHALLENGE), roots=[make_ca("Not Google").cert])

    def test_real_google_roots_do_not_trust_our_test_chain(self) -> None:
        device = AndroidDevice()
        with pytest.raises(AttestationError, match="trusted root"):
            verify(device, device.chain(CHALLENGE), roots=google_roots())

    def test_other_challenge_is_refused(self) -> None:
        device = AndroidDevice()
        with pytest.raises(AttestationError, match="challenge"):
            verify(device, device.chain(b"an old challenge"))

    def test_other_app_or_signing_key_is_refused(self) -> None:
        device = AndroidDevice()
        with pytest.raises(AttestationError, match="another app"):
            verify(device, device.chain(CHALLENGE, package="com.evil.clone"))
        with pytest.raises(AttestationError, match="release key"):
            verify(device, device.chain(CHALLENGE, signing_digest=b"\x01" * 32))

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"device_locked": False}, "unlocked"),
            ({"boot_state": 2}, "boot"),
            ({"root_of_trust": False}, "root of trust"),
        ],
    )
    def test_rooted_or_unlocked_devices_are_refused(
        self, overrides: dict[str, Any], match: str
    ) -> None:
        device = AndroidDevice()
        with pytest.raises(AttestationError, match=match):
            verify(device, device.chain(CHALLENGE, **overrides))

    def test_garbage_is_refused(self) -> None:
        with pytest.raises(AttestationError):
            verify(AndroidDevice(), [b"not a certificate"])


# --- Play Integrity ---------------------------------------------------------------------------

NONCE = "enrolment-challenge-0123456789"


def payload(**overrides: Any) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    base: dict[str, Any] = {
        "requestDetails": {
            "requestPackageName": PACKAGE,
            "nonce": NONCE,
            "timestampMillis": str(now_ms),
        },
        "appIntegrity": {"appRecognitionVerdict": "PLAY_RECOGNIZED", "packageName": PACKAGE},
        "deviceIntegrity": {"deviceRecognitionVerdict": ["MEETS_DEVICE_INTEGRITY"]},
    }
    for key, value in overrides.items():
        base[key] = base[key] | value
    return base


class TestPlayIntegrity:
    def test_good_verdict_passes(self) -> None:
        check_verdict(payload(), PACKAGE, NONCE, int(time.time() * 1000))

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"requestDetails": {"nonce": "other"}}, "challenge"),
            ({"requestDetails": {"requestPackageName": "com.evil"}}, "another app"),
            ({"requestDetails": {"timestampMillis": "1"}}, "stale"),
            ({"appIntegrity": {"appRecognitionVerdict": "UNRECOGNIZED_VERSION"}}, "not recognised"),
            (
                {"deviceIntegrity": {"deviceRecognitionVerdict": ["MEETS_BASIC_INTEGRITY"]}},
                "integrity",
            ),
        ],
    )
    def test_bad_verdicts_are_refused(self, overrides: dict[str, Any], match: str) -> None:
        with pytest.raises(AttestationError, match=match):
            check_verdict(payload(**overrides), PACKAGE, NONCE, int(time.time() * 1000))

    async def test_unconfigured_verifier_refuses_everything(self) -> None:
        with pytest.raises(AttestationError, match="not configured"):
            await NoIntegrityVerifier().verify("token", NONCE, 0)

    async def test_google_client_exchanges_a_token_and_decodes(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        account = {
            "client_email": "gateway@sigil.iam.gserviceaccount.com",
            "private_key": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            "token_uri": "https://oauth2.example/token",
        }
        seen: list[str] = []

        def google(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host + request.url.path)
            if request.url.host == "oauth2.example":
                return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
            assert request.headers["authorization"] == "Bearer at"
            assert json.loads(request.content) == {"integrity_token": "the-token"}
            return httpx.Response(200, json={"tokenPayloadExternal": payload()})

        client = GooglePlayIntegrity(PACKAGE, account, transport=httpx.MockTransport(google))
        await client.verify("the-token", NONCE, int(time.time() * 1000))
        await client.verify("the-token", NONCE, int(time.time() * 1000))
        assert seen == [
            "oauth2.example/token",
            f"playintegrity.googleapis.com/v1/{PACKAGE}:decodeIntegrityToken",
            f"playintegrity.googleapis.com/v1/{PACKAGE}:decodeIntegrityToken",
        ]

    async def test_google_refusal_is_an_attestation_failure(self) -> None:
        account = {"client_email": "x", "private_key": "not a key", "token_uri": "https://o/t"}
        client = GooglePlayIntegrity(
            PACKAGE, account, transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )
        with pytest.raises(AttestationError):
            await client.verify("t", NONCE, NOW.microsecond)
