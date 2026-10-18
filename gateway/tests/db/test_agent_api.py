import base64
from typing import Any

import httpx
import pytest

from gateway.contracts import agent
from gateway.db.models import Message
from gateway.enums import ThreadRole
from tests.conftest import TEST_ENV
from tests.db.world import AUSD, World

MAKER = {"Authorization": f"Bearer {TEST_ENV['PLATFORM_MAKER_TOKEN']}"}
AUDITOR = {"Authorization": f"Bearer {TEST_ENV['PLATFORM_AUDITOR_TOKEN']}"}


@pytest.fixture
def client(app_client: httpx.AsyncClient) -> httpx.AsyncClient:
    return app_client


def transfer_body(world: World, request_id: str, **overrides: Any) -> dict[str, Any]:
    body = world.instruction(None, request_id=request_id, idempotency_key=f"req:{request_id}:1")
    return body.model_dump(mode="json") | overrides


class TestAuth:
    async def test_missing_token_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/agent/v1/dependents")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_auditor_cannot_use_maker_routes(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/agent/v1/dependents", headers=AUDITOR)).status_code == 403

    async def test_maker_cannot_post_verdicts(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        """Only the Auditor's credential records a verdict, so the maker cannot forge an agree."""
        response = await client.post("/agent/v1/auditor/verdicts", headers=MAKER, json={})
        assert response.status_code == 403


class TestReads:
    async def test_dependents(self, client: httpx.AsyncClient, world: World) -> None:
        response = await client.get("/agent/v1/dependents", headers=MAKER)
        assert response.json() == [
            {"dependent_id": world.dependent_id, "guardian_id": str(world.family.guardian_user_id)}
        ]

    async def test_context(self, client: httpx.AsyncClient, world: World) -> None:
        response = await client.get(
            f"/agent/v1/dependents/{world.dependent_id}/context", headers=MAKER
        )
        context = agent.AgentContext.model_validate(response.json())
        assert context.policy.max_per_request == 40 * AUSD
        assert context.policy.allowance is not None
        assert context.budget.remaining_this_week == 100 * AUSD
        assert context.delegated_amount == 500 * AUSD
        assert context.frozen is False

    async def test_context_reports_no_delegation_to_another_delegate(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        world.crypto.delegate = "SomeoneElse111111111111111111111111111111111"
        response = await client.get(
            f"/agent/v1/dependents/{world.dependent_id}/context", headers=MAKER
        )
        assert response.json()["delegated_amount"] == 0

    async def test_request_with_thread_and_proofs(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        request = await world.add_request(10 * AUSD)
        proof = await world.add_proof(request, content=b"\xff\xd8jpeg")
        async with world.sessionmaker() as session:
            session.add(
                Message(request_id=request.id, role=ThreadRole.DEPENDENT, text="for notebooks")
            )
            await session.commit()

        response = await client.get(f"/agent/v1/requests/{request.id}", headers=MAKER)
        got = agent.DependentRequest.model_validate(response.json())
        assert (got.amount_requested, got.dependent_id) == (10 * AUSD, world.dependent_id)
        assert [m.text for m in got.thread] == ["for notebooks"]
        assert [p.id for p in got.proofs] == [str(proof.id)]

        stored = agent.Proof.model_validate(
            (await client.get(f"/agent/v1/proofs/{proof.id}", headers=MAKER)).json()
        )
        assert stored.data_b64 is not None
        assert base64.b64decode(stored.data_b64) == b"\xff\xd8jpeg"
        assert stored.size_bytes == 6 and stored.request_id == str(request.id)

    @pytest.mark.parametrize(
        "path",
        ["/agent/v1/requests/nope", "/agent/v1/proofs/0b8c", "/agent/v1/dependents/x/context"],
    )
    async def test_unknown_ids_are_404(self, client: httpx.AsyncClient, path: str) -> None:
        response = await client.get(path, headers=MAKER)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestTransfers:
    async def test_header_must_match_body(self, client: httpx.AsyncClient, world: World) -> None:
        request = await world.add_request(10 * AUSD)
        body = transfer_body(world, str(request.id))
        response = await client.post(
            "/agent/v1/transfers", headers=MAKER | {"Idempotency-Key": "other"}, json=body
        )
        assert response.status_code == 400

    async def test_recipient_field_is_rejected(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        """SEC-01."""
        request = await world.add_request(10 * AUSD)
        body = transfer_body(world, str(request.id), destination_token_account="Attacker111")
        response = await client.post(
            "/agent/v1/transfers",
            headers=MAKER | {"Idempotency-Key": body["idempotency_key"]},
            json=body,
        )
        assert response.status_code == 422

    async def test_reviewed_transfer_with_auditor_verdict(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        request = await world.add_request(10 * AUSD, emergency_claimed=True)
        body = transfer_body(world, str(request.id))
        verdict = {
            "dependent_id": world.dependent_id,
            "request_id": str(request.id),
            "idempotency_key": body["idempotency_key"],
            "amount": body["amount"],
            "category": body["category"],
            "verdict": "agree",
            "reason": "A real emergency that fits the guidance.",
            "concerns": [],
            "independent_assessment": {"stance": "supports", "notes": "Plausible."},
            "review_triggers": ["emergency_claimed"],
            "model": "auditor-model",
        }
        receipt = await client.post("/agent/v1/auditor/verdicts", headers=AUDITOR, json=verdict)
        verdict_id = receipt.json()["verdict_id"]

        headers = MAKER | {"Idempotency-Key": body["idempotency_key"]}
        result = await client.post(
            "/agent/v1/transfers", headers=headers, json=body | {"auditor_verdict_id": verdict_id}
        )
        assert result.json()["status"] == "sent"

        # The same verdict cannot back a second transfer.
        second = await world.add_request(10 * AUSD, emergency_claimed=True)
        reuse = transfer_body(world, str(second.id), auditor_verdict_id=verdict_id)
        result = await client.post(
            "/agent/v1/transfers",
            headers=MAKER | {"Idempotency-Key": reuse["idempotency_key"]},
            json=reuse,
        )
        assert result.json()["reason"] == "auditor verdict invalid"


class TestWrites:
    async def question(self, world: World, **overrides: Any) -> dict[str, Any]:
        request = await world.add_request(50 * AUSD)
        return {
            "guardian_id": str(world.family.guardian_user_id),
            "dependent_id": world.dependent_id,
            "request_id": str(request.id),
            "question": "50 for a school trip?",
            "recommendation": "approve",
            "recommended_amount": 50 * AUSD,
            "urgency": "normal",
            "agent_view": "Fits the guidance on school costs.",
            "dedupe_key": f"ask:{request.id}",
        } | overrides

    async def test_guardian_question_is_deduplicated(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        body = await self.question(world)
        first = await client.post("/agent/v1/guardian-questions", headers=MAKER, json=body)
        again = await client.post("/agent/v1/guardian-questions", headers=MAKER, json=body)
        assert first.json()["id"] == again.json()["id"]

    async def test_question_for_another_family_is_refused(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        body = await self.question(world, guardian_id=world.dependent_id)
        response = await client.post("/agent/v1/guardian-questions", headers=MAKER, json=body)
        assert response.status_code == 404

    async def test_decision_joins_the_thread_and_history(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        request = await world.add_request(10 * AUSD)
        decision = {
            "dependent_id": world.dependent_id,
            "request_id": str(request.id),
            "outcome": "declined",
            "reply_to_dependent": "Not this week, sorry.",
            "summary_for_guardian": "Declined a snack request.",
            "amount_sent": 0,
            "policy_version": 1,
        }
        assert (
            await client.post("/agent/v1/decisions", headers=MAKER, json=decision)
        ).status_code == 200

        got = (await client.get(f"/agent/v1/requests/{request.id}", headers=MAKER)).json()
        last = got["thread"][-1]
        assert (last["role"], last["text"]) == ("agent", "Not this week, sorry.")
        context = (
            await client.get(f"/agent/v1/dependents/{world.dependent_id}/context", headers=MAKER)
        ).json()
        assert [h["kind"] for h in context["history"]] == ["declined"]

    async def test_usage_is_counted_per_family(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        guardian = str(world.family.guardian_user_id)
        report = {"family_id": guardian, "input_tokens": 300, "output_tokens": 200}
        after = await client.post("/agent/v1/usage", headers=MAKER, json=report)
        status = await client.get(f"/agent/v1/usage/{guardian}", headers=MAKER)
        assert after.json() == status.json() == {"remaining_tokens": 200_000 - 500}

    async def test_policy_draft_validation(self, client: httpx.AsyncClient, world: World) -> None:
        draft = {
            "weekly_cap": 100.0,
            "max_per_request": 40.0,
            "proof_threshold": 25.0,
            "allowance_amount": 30.0,
            "allowance_weekday": 0,
            "guidance": "Books are fine.",
        }
        submission = {
            "guardian_id": str(world.family.guardian_user_id),
            "dependent_id": world.dependent_id,
            "draft": draft,
        }
        ok = await client.post("/agent/v1/policy-drafts/validate", headers=MAKER, json=submission)
        assert ok.json() == {"valid": True, "errors": []}
        bad = submission | {"draft": draft | {"max_per_request": 200.0}}
        result = await client.post("/agent/v1/policy-drafts/validate", headers=MAKER, json=bad)
        assert result.json()["valid"] is False and result.json()["errors"]

    async def test_notifications_dedupe_and_events(
        self, client: httpx.AsyncClient, world: World
    ) -> None:
        note = {
            "audience": "guardian",
            "recipient_id": str(world.family.guardian_user_id),
            "message": "Reservoir runs out in about 2 weeks.",
            "dedupe_key": "reservoir:2026-W42",
        }
        first = await client.post("/agent/v1/notifications", headers=MAKER, json=note)
        again = await client.post("/agent/v1/notifications", headers=MAKER, json=note)
        assert first.json()["id"] == again.json()["id"]
        event = {"kind": "guardrails", "subject_id": "r1", "detail": {"events": ["x"]}}
        assert (await client.post("/agent/v1/events", headers=MAKER, json=event)).status_code == 200
