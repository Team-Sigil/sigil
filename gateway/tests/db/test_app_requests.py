import base64
import hashlib
import io
from typing import Any

import httpx
import pytest
from nacl.signing import SigningKey
from PIL import Image
from sqlalchemy import select, text

from gateway.contracts.signing import answer_question_message, freeze_message
from gateway.core.config import Settings
from gateway.db.models import GuardianQuestion, Proof
from gateway.enums import GuardianDecision
from gateway.main import create_app
from tests.db.test_agent_api import MAKER
from tests.db.test_app_auth import login, sign
from tests.db.test_app_families_policy import TestFamilies, nonce
from tests.db.world import AGENT_WALLET, AUSD, MINT, World
from tests.images import jpeg, jpeg_of, picture, png

JPEG = jpeg(1)
PNG = png(2)


async def new_request(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body = {"message": "Can I have 10 for notebooks?", "amount_requested": 10 * AUSD} | overrides
    response = await client.post("/api/v1/requests", headers=headers, json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    request_id: str,
    data: bytes,
    content_type: str = "image/jpeg",
) -> httpx.Response:
    return await client.post(
        f"/api/v1/requests/{request_id}/proofs",
        headers=headers,
        files={"file": ("receipt", data, content_type)},
        data={"kind": "receipt"},
    )


class TestRequests:
    async def test_new_request_runs_the_agent(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        result = await new_request(app_client, headers)
        assert result["decision"] == {
            "outcome": "asked_dependent",
            "reply_to_dependent": "What is it for?",
            "amount_sent": 0,
            "transfer_id": None,
            "summary_for_guardian": None,
            "created_at": None,
        }
        assert world.agent.processed == [result["request"]["id"]]

        detail = (
            await app_client.get(f"/api/v1/requests/{result['request']['id']}", headers=headers)
        ).json()
        assert [(t["role"], t["text"]) for t in detail["thread"]] == [
            ("dependent", "Can I have 10 for notebooks?")
        ]

    async def test_follow_up_message_can_set_the_amount(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        created = await new_request(app_client, headers, amount_requested=None)
        request_id = created["request"]["id"]
        follow_up = await app_client.post(
            f"/api/v1/requests/{request_id}/messages",
            headers=headers,
            json={"message": "12 please", "amount_requested": 12 * AUSD},
        )
        assert follow_up.json()["request"]["amount_requested"] == 12 * AUSD
        assert world.agent.processed == [request_id, request_id]

    @pytest.mark.parametrize("amount", [10.5, "10000000", 0, -1])
    async def test_amount_must_be_positive_base_units(
        self, app_client: httpx.AsyncClient, world: World, amount: object
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        response = await app_client.post(
            "/api/v1/requests", headers=headers, json={"message": "hi", "amount_requested": amount}
        )
        assert response.status_code == 422

    async def test_guardian_cannot_create_requests(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.guardian_key)
        response = await app_client.post("/api/v1/requests", headers=headers, json={"message": "x"})
        assert response.status_code == 403

    async def test_other_familys_request_is_invisible(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """SEC-10: another family's guardian gets 404, not the request."""
        created = await new_request(app_client, await login(app_client, world.dependent_key))
        other_guardian = SigningKey.generate()
        registered = await TestFamilies().register(app_client, other_guardian)
        stranger = {"Authorization": f"Bearer {registered.json()['token']}"}
        response = await app_client.get(
            f"/api/v1/requests/{created['request']['id']}", headers=stranger
        )
        assert response.status_code == 404

    async def test_messages_are_encrypted_at_rest(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        await new_request(app_client, await login(app_client, world.dependent_key))
        async with world.sessionmaker() as session:
            raw = (await session.execute(text("SELECT text FROM messages"))).scalars().all()
        assert raw and all(b"notebooks" not in r for r in raw)

    async def test_agent_outage_keeps_the_request(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        world.agent.fail = True
        headers = await login(app_client, world.dependent_key)
        response = await app_client.post(
            "/api/v1/requests", headers=headers, json={"message": "notebooks please"}
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AGENT_UNAVAILABLE"
        listed = (await app_client.get("/api/v1/requests", headers=headers)).json()
        assert [r["message"] for r in listed] == ["notebooks please"]


class TestProofs:
    async def test_image_proof_is_typed_by_its_bytes_and_hashed(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        # The client claims PNG; the bytes are a JPEG.
        response = await upload(app_client, headers, request_id, JPEG, content_type="image/png")
        assert response.status_code == 200, response.text
        assert response.json()["proof"]["media_type"] == "image/jpeg"
        async with world.sessionmaker() as session:
            stored = await session.scalar(select(Proof))
        assert stored is not None
        # The agent sees the normalised copy; the upload is kept, encrypted, as evidence.
        assert stored.original_sha256 == hashlib.sha256(JPEG).hexdigest()
        assert stored.sha256 == hashlib.sha256(stored.content).hexdigest()
        assert stored.original == JPEG and stored.source == "file"

    async def test_reupload_is_flagged_as_duplicate(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        first = (await new_request(app_client, headers))["request"]["id"]
        second = (await new_request(app_client, headers))["request"]["id"]
        await upload(app_client, headers, first, JPEG)
        again = await upload(app_client, headers, second, JPEG)
        assert again.json()["proof"]["flags"] == ["duplicate"]

    async def test_text_is_a_typed_claim(self, app_client: httpx.AsyncClient, world: World) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        response = await upload(
            app_client,
            headers,
            request_id,
            b"Total 10.00",
            content_type="text/plain; charset=utf-8",
        )
        assert response.json()["proof"]["media_type"] == "text/plain"

    @pytest.mark.parametrize(
        ("data", "content_type"),
        # PDFs are accepted now (online orders); see test_proof_obligations.TestOnlineOrders.
        [(b"<script>alert(1)</script>", "image/jpeg"), (b"PK\x03\x04zip", "application/zip")],
    )
    async def test_non_images_are_refused(
        self, app_client: httpx.AsyncClient, world: World, data: bytes, content_type: str
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        response = await upload(app_client, headers, request_id, data, content_type)
        assert response.status_code == 422

    async def test_size_limit_and_no_count_limit(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        too_big = JPEG + b"0" * (15 * 1024 * 1024)
        assert (await upload(app_client, headers, request_id, too_big)).status_code == 413
        for i in range(5):  # any number of proofs; the daily message quota bounds them
            response = await upload(app_client, headers, request_id, png(10 + i))
            assert response.status_code == 200


class TestGuardianAnswers:
    async def open_question(self, world: World, request_id: str) -> GuardianQuestion:
        async with world.sessionmaker() as session:
            question = GuardianQuestion(
                family_id=world.family.id,
                request_id=request_id,
                question="10 for notebooks?",
                recommendation="approve",
                recommended_amount=10 * AUSD,
                urgency="normal",
                agent_view="Fits the guidance.",
                dedupe_key=f"ask:{request_id}",
            )
            session.add(question)
            await session.commit()
            return question

    async def answer(
        self,
        client: httpx.AsyncClient,
        world: World,
        question: GuardianQuestion,
        decision: GuardianDecision,
        amount: int | None,
        key: SigningKey | None = None,
    ) -> httpx.Response:
        headers = await login(client, world.guardian_key)
        n = await nonce(client, world.guardian_key, "answer-question")
        message = answer_question_message(world.family.id, question.id, decision, amount, n)
        body: dict[str, Any] = {
            "decision": decision.value,
            "nonce": n,
            "signature_b58": sign(key or world.guardian_key, message),
        }
        if amount is not None:
            body["amount_units"] = amount
        return await client.post(
            f"/api/v1/questions/{question.id}/answer", headers=headers, json=body
        )

    async def test_signed_approval_reaches_the_thread_and_reruns_the_agent(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        request_id = (await new_request(app_client, await login(app_client, world.dependent_key)))[
            "request"
        ]["id"]
        question = await self.open_question(world, request_id)
        response = await self.answer(
            app_client, world, question, GuardianDecision.APPROVE, 8 * AUSD
        )
        assert response.status_code == 200, response.text
        assert response.json()["question"]["approved_amount"] == 8 * AUSD
        assert world.agent.processed == [request_id, request_id]

        thread = (
            await app_client.get(
                f"/api/v1/requests/{request_id}",
                headers=await login(app_client, world.guardian_key),
            )
        ).json()["thread"]
        assert thread[-1] == thread[-1] | {
            "role": "guardian_answer",
            "text": "The guardian approved 8.00.",
        }

        again = await self.answer(app_client, world, question, GuardianDecision.DENY, None)
        assert again.status_code == 409

    async def test_forged_answer_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """SEC-05: only the guardian's wallet can answer."""
        request_id = (await new_request(app_client, await login(app_client, world.dependent_key)))[
            "request"
        ]["id"]
        question = await self.open_question(world, request_id)
        response = await self.answer(
            app_client,
            world,
            question,
            GuardianDecision.APPROVE,
            8 * AUSD,
            key=SigningKey.generate(),
        )
        assert response.status_code == 401

    async def test_cannot_approve_more_than_requested(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        request_id = (await new_request(app_client, await login(app_client, world.dependent_key)))[
            "request"
        ]["id"]
        question = await self.open_question(world, request_id)
        response = await self.answer(
            app_client, world, question, GuardianDecision.APPROVE, 11 * AUSD
        )
        assert response.status_code == 422

    async def test_dependent_cannot_answer(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        question = await self.open_question(world, request_id)
        response = await app_client.post(
            f"/api/v1/questions/{question.id}/answer",
            headers=headers,
            json={"decision": "deny", "nonce": "n", "signature_b58": "s"},
        )
        assert response.status_code == 403


class TestFreeze:
    async def freeze(
        self, client: httpx.AsyncClient, world: World, frozen: bool, key: SigningKey | None = None
    ) -> httpx.Response:
        headers = await login(client, world.guardian_key)
        n = await nonce(client, world.guardian_key, "freeze")
        signature = sign(key or world.guardian_key, freeze_message(world.family.id, frozen, n))
        return await client.post(
            "/api/v1/freeze",
            headers=headers,
            json={"frozen": frozen, "nonce": n, "signature_b58": signature},
        )

    async def test_signed_freeze_blocks_transfers_until_unfrozen(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """SEC-17."""
        assert (await self.freeze(app_client, world, True)).json() == {"frozen": True}
        request = await world.add_request(10 * AUSD)
        blocked = await world.engine.submit(world.instruction(request))
        assert blocked.reason == "frozen by guardian"

        assert (await self.freeze(app_client, world, False)).json() == {"frozen": False}
        other = await world.add_request(10 * AUSD)
        assert (await world.engine.submit(world.instruction(other))).status == "sent"

    async def test_unsigned_freeze_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        response = await self.freeze(app_client, world, True, key=SigningKey.generate())
        assert response.status_code == 401


async def test_dependent_daily_message_limit(world: World) -> None:
    """Agent handoff item 8: each message runs the agent, so a dependent gets a daily quota."""
    settings = Settings(
        agent_wallet_pubkey=AGENT_WALLET, ausd_mint=MINT, dependent_messages_per_day=2
    )
    app = create_app(
        settings=settings, sessionmaker=world.sessionmaker, crypto=world.crypto, agent=world.agent
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        headers = await login(client, world.dependent_key)
        request_id = (await new_request(client, headers))["request"]["id"]
        second = await client.post(
            f"/api/v1/requests/{request_id}/messages",
            headers=headers,
            json={"message": "for school"},
        )
        assert second.status_code == 200
        third = await client.post(
            "/api/v1/requests", headers=headers, json={"message": "and lunch"}
        )
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "DAILY_LIMIT_REACHED"
        assert len(world.agent.processed) == 2  # the refused message never reached the agent


class TestNormalisation:
    """Agent handoff items 4 and 6: what the agent receives, and the integrity flags."""

    async def test_agent_receives_a_stripped_copy(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        exif = Image.Exif()
        exif[0x010F], exif[0x0110] = "Apple", "iPhone 16"
        exif.get_ifd(0x8825)[2] = (13.0, 45.0, 0.0)  # GPS latitude: the dependent's location
        out = io.BytesIO()
        picture(4, (3000, 4000)).save(out, format="JPEG", exif=exif.tobytes())

        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        proof = (await upload(app_client, headers, request_id, out.getvalue())).json()["proof"]
        seen = (await app_client.get(f"/agent/v1/proofs/{proof['id']}", headers=MAKER)).json()
        stored = Image.open(io.BytesIO(base64.b64decode(seen["data_b64"])))
        assert dict(stored.getexif()) == {} and max(stored.size) == 2576
        assert seen["size_bytes"] == len(base64.b64decode(seen["data_b64"]))

    async def test_rephotographed_receipt_is_near_duplicate(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        first = (await new_request(app_client, headers))["request"]["id"]
        second = (await new_request(app_client, headers))["request"]["id"]
        receipt = picture(7, (1200, 1600))
        await upload(app_client, headers, first, jpeg_of(receipt))
        # The same receipt again, smaller and re-compressed: a new SHA-256, the same picture.
        again = await upload(app_client, headers, second, jpeg_of(receipt.resize((900, 1200))))
        assert again.json()["proof"]["flags"] == ["near_duplicate"]

    async def test_screenshot_is_flagged(self, app_client: httpx.AsyncClient, world: World) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        response = await upload(app_client, headers, request_id, png(8, (1170, 2532)), "image/png")
        assert {"no_camera_metadata", "screenshot"} <= set(response.json()["proof"]["flags"])

    async def test_gif_is_refused(self, app_client: httpx.AsyncClient, world: World) -> None:
        out = io.BytesIO()
        picture(9).save(out, format="GIF")
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        response = await upload(app_client, headers, request_id, out.getvalue(), "image/gif")
        assert response.status_code == 422
