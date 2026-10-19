"""The agent layer's own HTTP clients against this gateway, over real HTTP.

`agent_layer.platform_api` validates every response against the agent's contract, caps its
size and fails closed on anything unexpected, so if these calls succeed the agent can run
against this gateway. Needs the agent layer (see tests/agent_layer.py).
"""

import threading
import time
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from gateway.core.config import Settings
from gateway.db.session import make_sessionmaker
from gateway.main import create_app
from tests.agent_layer import import_agent_layer
from tests.conftest import TEST_ENV
from tests.db.conftest import APP_URL
from tests.db.world import AGENT_WALLET, AUSD, MINT, World


class BackgroundServer:
    def __init__(self, app: FastAPI) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> str:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def agent_layer() -> tuple[ModuleType, ModuleType]:
    return (
        import_agent_layer("agent_layer.contracts"),
        import_agent_layer("agent_layer.platform_api"),
    )


@pytest.fixture
def gateway_url(world: World) -> Iterator[str]:
    assert APP_URL is not None
    settings = Settings(agent_wallet_pubkey=AGENT_WALLET, ausd_mint=MINT)
    # Its own connection pool: the server runs on its own event loop in another thread.
    app = create_app(
        settings=settings,
        sessionmaker=make_sessionmaker(APP_URL),
        crypto=world.crypto,
        agent=world.agent,
    )
    server = BackgroundServer(app)
    url = server.start()
    yield url
    server.stop()


async def test_agent_layer_clients_work_against_the_gateway(
    world: World, gateway_url: str, agent_layer: tuple[ModuleType, ModuleType]
) -> None:
    c, api = agent_layer
    maker = api.MakerAPI(gateway_url, TEST_ENV["PLATFORM_MAKER_TOKEN"])
    auditor = api.AuditorAPI(gateway_url, TEST_ENV["PLATFORM_AUDITOR_TOKEN"])
    dep, guardian = world.dependent_id, str(world.family.guardian_user_id)
    try:
        assert [d.dependent_id for d in maker.list_dependents()] == [dep]
        context = maker.get_context(dep)
        assert (context.policy.version, context.delegated_amount) == (1, 500 * AUSD)

        stored = await world.add_request(10 * AUSD, emergency_claimed=True)
        proof_row = await world.add_proof(stored)
        request = maker.get_request(str(stored.id))
        proof = maker.get_proof(request.proofs[0].id)
        assert proof.sha256 == proof_row.sha256 and proof.request_id == request.id

        # The maker's key scheme; an emergency needs a verdict from the Auditor's token.
        key = f"req:{request.id}:{len(request.thread)}"
        verdict: Any = c.VerdictSubmission(
            dependent_id=dep,
            request_id=request.id,
            idempotency_key=key,
            amount=8 * AUSD,
            category=c.Category.EDUCATION,
            verdict="agree",
            reason="Plausible emergency within the guidance.",
            concerns=[],
            independent_assessment=c.IndependentAssessment(stance="supports", notes="Fine."),
            review_triggers=["emergency_claimed"],
            model="auditor-model",
        )
        with pytest.raises(api.PlatformAPIError) as refused:
            api.AuditorAPI(gateway_url, TEST_ENV["PLATFORM_MAKER_TOKEN"]).submit_verdict(verdict)
        assert refused.value.status == 403
        verdict_id = auditor.submit_verdict(verdict).verdict_id

        instruction = c.TransferInstruction(
            dependent_id=dep,
            request_id=request.id,
            trigger="request",
            amount=8 * AUSD,
            purpose="notebooks",
            category=c.Category.EDUCATION,
            category_confidence=0.9,
            reasoning="Fits the guidance for school costs.",
            idempotency_key=key,
            policy_version=context.policy.version,
            auditor_verdict_id=verdict_id,
        )
        assert maker.submit_transfer(instruction).status == "sent"
        assert maker.submit_transfer(instruction).status == "duplicate"
        assert maker.get_request(request.id).already_sent == 8 * AUSD

        maker.record_decision(
            c.DecisionRecord(
                dependent_id=dep,
                request_id=request.id,
                outcome=c.Outcome.SENT_FULL,
                reply_to_dependent="Sent 8.00 for your notebooks.",
                summary_for_guardian="Sent 8.00 for notebooks.",
                amount_sent=8 * AUSD,
                policy_version=1,
            )
        )
        assert maker.get_request(request.id).thread[-1].role == "agent"
        assert [h.kind for h in maker.get_weekly_activity(dep).items] == ["transfer"]

        maker.ask_guardian(
            c.GuardianQuestion(
                guardian_id=guardian,
                dependent_id=dep,
                request_id=request.id,
                question="Another 2 for pens?",
                recommendation="approve",
                agent_view="Small and school-related.",
                dedupe_key=f"ask:{key}",
            )
        )
        maker.request_proof(
            c.ProofRequest(request_id=request.id, dependent_id=dep, kind="receipt", reason="Total?")
        )
        maker.propose_policy_update(
            c.PolicyProposal(
                guardian_id=guardian,
                dependent_id=dep,
                change="Stop asking about books under 15.",
                evidence="Approved 3 book requests.",
                dedupe_key="learn:books",
            )
        )
        maker.notify(c.Notification(audience="dependent", recipient_id=dep, message="Slow down."))
        maker.log_event(c.AgentEvent(kind="guardrails", subject_id=request.id))
        assert maker.get_guardian_answers(guardian) == []
        remaining = maker.report_usage(
            c.UsageReport(family_id=guardian, input_tokens=100, output_tokens=50)
        ).remaining_tokens
        assert maker.get_usage(guardian).remaining_tokens == remaining
        draft = c.PolicyDraft(
            weekly_cap=100, max_per_request=40, proof_threshold=25, guidance="Books are fine."
        )
        validation = maker.validate_policy_draft(
            c.PolicyDraftSubmission(guardian_id=guardian, dependent_id=dep, draft=draft)
        )
        assert validation.valid
    finally:
        maker.close()
        auditor.close()
