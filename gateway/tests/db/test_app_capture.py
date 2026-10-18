import base64
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from gateway.capture.config import CaptureConfig
from gateway.contracts.agent import ProofFindingsRecord
from gateway.core.config import Settings
from gateway.db.models import CaptureTicket, Request
from gateway.engine.transfers import TransferEngine
from gateway.main import create_app
from tests.capture_fakes import APP_ID, AppleDevice
from tests.db.test_app_auth import login
from tests.db.test_app_requests import JPEG, new_request
from tests.db.world import AGENT_WALLET, AUSD, MINT, World
from tests.images import png


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.fixture
def iphone() -> AppleDevice:
    return AppleDevice()


@pytest.fixture
async def client(world: World, iphone: AppleDevice) -> AsyncIterator[httpx.AsyncClient]:
    capture = CaptureConfig(
        apple_app_id=APP_ID,
        apple_production=True,
        apple_roots=[iphone.root.cert],  # stand-in for Apple's root
    )
    app = create_app(
        settings=Settings(agent_wallet_pubkey=AGENT_WALLET, ausd_mint=MINT),
        sessionmaker=world.sessionmaker,
        crypto=world.crypto,
        agent=world.agent,
        capture=capture,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


async def challenge(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    value: str = (await client.post("/api/v1/capture/challenge", headers=headers)).json()[
        "challenge"
    ]
    return value


async def enrol_iphone(
    client: httpx.AsyncClient, headers: dict[str, str], phone: AppleDevice, **attest: Any
) -> httpx.Response:
    c = await challenge(client, headers)
    body = {
        "challenge": c,
        "key_id": b64(phone.key_id),
        "attestation_object": b64(phone.attest(attest.pop("challenge", c).encode(), **attest)),
    }
    return await client.post("/api/v1/capture/devices", headers=headers, json=body)


async def ticket(
    client: httpx.AsyncClient, headers: dict[str, str], request_id: str, device_id: str
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/requests/{request_id}/capture-tickets",
        headers=headers,
        json={"device_id": device_id},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def claim(
    data: bytes, nonce: str, request_id: str, key_id: str, captured_at: datetime | None = None
) -> str:
    return json.dumps(
        {
            "sha256": hashlib.sha256(data).hexdigest(),
            "nonce": nonce,
            "request_id": request_id,
            "captured_at": (captured_at or datetime.now(UTC)).isoformat(),
            "device_key_id": key_id,
            "app_version": "1.0.0",
        }
    )


async def upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    request_id: str,
    data: bytes,
    claim_text: str | None = None,
    signature: str | None = None,
) -> dict[str, Any]:
    fields = {"kind": "receipt"}
    if claim_text is not None and signature is not None:
        fields |= {"capture_claim": claim_text, "capture_signature": signature}
    response = await client.post(
        f"/api/v1/requests/{request_id}/proofs",
        headers=headers,
        files={"file": ("receipt.jpg", data, "image/jpeg")},
        data=fields,
    )
    assert response.status_code == 200, response.text
    proof: dict[str, Any] = response.json()["proof"]
    return proof


class TestEnrolment:
    async def test_iphone_enrols(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers = await login(client, world.dependent_key)
        response = await enrol_iphone(client, headers, iphone)
        assert response.status_code == 200, response.text
        listed = (await client.get("/api/v1/capture/devices", headers=headers)).json()
        assert [d["key_id"] for d in listed] == [b64(iphone.key_id)]

    async def test_attestation_for_an_old_challenge_is_refused(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers = await login(client, world.dependent_key)
        response = await enrol_iphone(client, headers, iphone, challenge="stale-challenge")
        assert response.status_code == 422
        assert (await client.get("/api/v1/capture/devices", headers=headers)).json() == []

    async def test_challenge_works_once(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers = await login(client, world.dependent_key)
        c = await challenge(client, headers)
        body = {
            "challenge": c,
            "key_id": b64(iphone.key_id),
            "attestation_object": b64(iphone.attest(c.encode())),
        }
        assert (
            await client.post("/api/v1/capture/devices", headers=headers, json=body)
        ).status_code == 200
        assert (
            await client.post("/api/v1/capture/devices", headers=headers, json=body)
        ).status_code == 401

    async def test_unconfigured_app_attest_is_refused(
        self, world: World, iphone: AppleDevice
    ) -> None:
        app = create_app(
            settings=Settings(agent_wallet_pubkey=AGENT_WALLET, ausd_mint=MINT),
            sessionmaker=world.sessionmaker,
            crypto=world.crypto,
            agent=world.agent,
            capture=CaptureConfig(None, True, apple_roots=[iphone.root.cert]),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
            headers = await login(c, world.dependent_key)
            assert (await enrol_iphone(c, headers, iphone)).status_code == 422

    async def test_guardian_cannot_enrol(self, client: httpx.AsyncClient, world: World) -> None:
        headers = await login(client, world.guardian_key)
        assert (await client.post("/api/v1/capture/challenge", headers=headers)).status_code == 403


class TestIphoneCapture:
    async def setup(
        self, client: httpx.AsyncClient, world: World, phone: AppleDevice
    ) -> tuple[dict[str, str], str, str]:
        headers = await login(client, world.dependent_key)
        device = (await enrol_iphone(client, headers, phone)).json()
        request_id = (await new_request(client, headers))["request"]["id"]
        return headers, request_id, device["id"]

    async def test_signed_capture_becomes_a_photo(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        proof = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        assert (proof["source"], proof["flags"]) == ("photo", ["camera_signed"])
        assert proof["captured_at"] is not None

    async def test_plain_upload_stays_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, _ = await self.setup(client, world, iphone)
        assert (await upload(client, headers, request_id, JPEG))["source"] == "file"

    async def test_edited_photo_is_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        """The bytes no longer match the hash the device signed."""
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        edited = JPEG + b"edited"
        proof = await upload(
            client, headers, request_id, edited, text, b64(iphone.assert_(text.encode()))
        )
        assert proof["source"] == "file"

    async def test_ticket_works_once(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        first = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        again = await upload(
            client,
            headers,
            request_id,
            JPEG + b"2",
            text,
            b64(iphone.assert_(text.encode())),
        )
        assert (first["source"], again["source"]) == ("photo", "file")

    async def test_failed_capture_still_burns_the_ticket(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        await upload(
            client,
            headers,
            request_id,
            JPEG + b"edited",
            text,
            b64(iphone.assert_(text.encode())),
        )
        retry = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        assert retry["source"] == "file"

    async def test_expired_ticket_is_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        async with world.sessionmaker() as session:
            await session.execute(
                update(CaptureTicket).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        proof = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        assert proof["source"] == "file"

    async def test_capture_time_outside_the_ticket_is_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        old = datetime.now(UTC) - timedelta(days=3)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id), captured_at=old)
        proof = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        assert proof["source"] == "file"

    async def test_ticket_for_another_request_is_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        other = (await new_request(client, headers))["request"]["id"]
        t = await ticket(client, headers, other, device_id)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        proof = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        assert proof["source"] == "file"

    async def test_replayed_assertion_is_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        """App Attest's counter must increase; an old assertion cannot be reused."""
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t1 = await ticket(client, headers, request_id, device_id)
        text1 = claim(JPEG, t1["ticket"], request_id, b64(iphone.key_id))
        await upload(client, headers, request_id, JPEG, text1, b64(iphone.assert_(text1.encode())))
        t2 = await ticket(client, headers, request_id, device_id)
        text2 = claim(PNG_LIKE, t2["ticket"], request_id, b64(iphone.key_id))
        replay = b64(iphone.assert_(text2.encode(), counter=1))
        assert (await upload(client, headers, request_id, PNG_LIKE, text2, replay))[
            "source"
        ] == "file"

    async def test_another_phones_signature_is_a_file(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        forged = b64(AppleDevice().assert_(text.encode()))
        assert (await upload(client, headers, request_id, JPEG, text, forged))["source"] == "file"

    async def test_revoked_device_cannot_capture(
        self, client: httpx.AsyncClient, world: World, iphone: AppleDevice
    ) -> None:
        headers, request_id, device_id = await self.setup(client, world, iphone)
        t = await ticket(client, headers, request_id, device_id)
        guardian = await login(client, world.guardian_key)
        revoked = await client.delete(f"/api/v1/capture/devices/{device_id}", headers=guardian)
        assert revoked.json()["revoked_at"] is not None
        text = claim(JPEG, t["ticket"], request_id, b64(iphone.key_id))
        proof = await upload(
            client, headers, request_id, JPEG, text, b64(iphone.assert_(text.encode()))
        )
        assert proof["source"] == "file"
        refused = await client.post(
            f"/api/v1/requests/{request_id}/capture-tickets",
            headers=headers,
            json={"device_id": device_id},
        )
        assert refused.status_code == 409


PNG_LIKE = png(5)


class TestCameraOnlyEngine:
    """CAMERA_ONLY_PROOFS: only verified captures (or OCR read from one) count as proof."""

    def engine(self, world: World) -> TransferEngine:
        return TransferEngine(
            world.sessionmaker, world.crypto, agent_wallet=AGENT_WALLET, mint=MINT, camera_only=True
        )

    async def transfer(self, world: World, proof_ids: list[str], request: Request) -> str | None:
        result = await self.engine(world).submit(
            world.instruction(
                request,
                amount=30 * AUSD,
                proof_ids=proof_ids,
                proof_findings=ProofFindingsRecord(
                    merchant="Book Shop", date="2026-10-14", total=30 * AUSD, matches_claim=True
                ),
            )
        )
        return result.reason if result.status == "blocked" else result.status

    async def test_plain_file_is_not_proof(self, world: World) -> None:
        request = await world.add_request(30 * AUSD)
        proof = await world.add_proof(request, source="file")
        assert await self.transfer(world, [str(proof.id)], request) == "proof required"

    async def test_verified_photo_is_proof(self, world: World) -> None:
        request = await world.add_request(30 * AUSD)
        proof = await world.add_proof(request, source="photo")
        assert await self.transfer(world, [str(proof.id)], request) == "sent"

    async def test_ocr_counts_only_when_read_from_a_verified_photo(self, world: World) -> None:
        request = await world.add_request(30 * AUSD)
        photo = await world.add_proof(request, b"photo", source="photo")
        ocr = await world.add_proof(
            request, b"Total 30.00", source="ocr", media_type="text/plain", derived_from=photo
        )
        assert await self.transfer(world, [str(ocr.id)], request) == "sent"

        other = await world.add_request(30 * AUSD)
        upload = await world.add_proof(other, b"upload", source="file")
        ocr_of_file = await world.add_proof(
            other, b"Total 30.00 again", source="ocr", media_type="text/plain", derived_from=upload
        )
        assert await self.transfer(world, [str(ocr_of_file.id)], other) == "proof required"
