import hashlib
from typing import Any

import httpx
import pytest
from nacl.signing import SigningKey
from sqlalchemy import select, text

from gateway.contracts.signing import answer_question_message, freeze_message
from gateway.db.models import GuardianQuestion, Proof
from gateway.enums import GuardianDecision
from tests.db.test_app_auth import login, sign
from tests.db.test_app_families_policy import TestFamilies, nonce
from tests.db.world import AUSD, World

JPEG = b"\xff\xd8\xff\xe0" + b"receipt-bytes" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 10


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
        assert stored.sha256 == hashlib.sha256(JPEG).hexdigest() and stored.source == "file"

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
        [(b"<script>alert(1)</script>", "image/jpeg"), (b"%PDF-1.7", "application/pdf")],
    )
    async def test_non_images_are_refused(
        self, app_client: httpx.AsyncClient, world: World, data: bytes, content_type: str
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        response = await upload(app_client, headers, request_id, data, content_type)
        assert response.status_code == 422

    async def test_size_and_count_limits(self, app_client: httpx.AsyncClient, world: World) -> None:
        headers = await login(app_client, world.dependent_key)
        request_id = (await new_request(app_client, headers))["request"]["id"]
        too_big = JPEG + b"0" * (5 * 1024 * 1024)
        assert (await upload(app_client, headers, request_id, too_big)).status_code == 413
        for i in range(3):
            response = await upload(app_client, headers, request_id, PNG + bytes([i]))
            assert response.status_code == 200
        assert (await upload(app_client, headers, request_id, PNG + b"x")).status_code == 409


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
