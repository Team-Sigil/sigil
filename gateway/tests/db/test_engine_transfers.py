import asyncio
import uuid

from nacl.signing import SigningKey
from sqlalchemy import select, update

from gateway.contracts.agent import ProofFindingsRecord, TransferInstruction
from gateway.contracts.policy import policy_hash
from gateway.contracts.signing import answer_question_message
from gateway.db import audit_log
from gateway.db.models import BudgetCounter, Family, GuardianQuestion, Policy, Request, Transfer
from gateway.engine.transfers import SIGNER_UNAVAILABLE
from gateway.enums import GuardianDecision, QuestionStatus, TransferStatus
from tests.db.world import AUSD, POLICY, WEEK, World, wallet


async def test_cleared_transfer_is_sent_and_settled(world: World) -> None:
    request = await world.add_request(10 * AUSD)
    result = await world.engine.submit(world.instruction(request))

    assert result.status == "sent" and result.amount == 8 * AUSD and result.tx_signature
    sent = world.crypto.calls[0]
    assert sent.destination_token_account == world.family.dependent_token_account
    assert sent.source_token_account == world.family.guardian_token_account
    budget = await world.budget()
    assert (budget.week_start, budget.spent, budget.reserved) == (WEEK, 8 * AUSD, 0)
    async with world.sessionmaker() as session:
        stored = await session.scalar(select(Request).where(Request.id == request.id))
        assert stored is not None and stored.already_sent == 8 * AUSD
        events = [e.event_type for e in await audit_log.load_chain(session, world.family.id)]
        assert events == ["transfer_pending", "transfer_sent"]
        assert await audit_log.verify_chain(session, world.family.id)


async def test_replay_never_pays_twice(world: World) -> None:
    """SEC-07 / attack demo 3."""
    request = await world.add_request(10 * AUSD)
    first = await world.engine.submit(world.instruction(request))
    again = await world.engine.submit(world.instruction(request))
    assert again.status == "duplicate" and again.decision_id == first.decision_id
    assert len(world.crypto.calls) == 1
    assert (await world.budget()).spent == 8 * AUSD


async def test_blocked_decision_is_stored_and_replayed(world: World) -> None:
    request = await world.add_request(50 * AUSD)
    t = world.instruction(request, amount=41 * AUSD)
    first = await world.engine.submit(t)
    again = await world.engine.submit(t)
    assert (first.status, first.reason) == ("blocked", "over max per request")
    assert again.decision_id == first.decision_id and again.reason == first.reason
    assert world.crypto.calls == []
    assert (await world.budget()).reserved == 0


async def test_parallel_transfers_cannot_exceed_the_weekly_cap(world: World) -> None:
    """SEC-08: the budget row lock serializes them; only one fits."""
    async with world.sessionmaker() as session:
        session.add(BudgetCounter(family_id=world.family.id, week_start=WEEK, spent=60 * AUSD))
        await session.commit()
    requests = [await world.add_request(24 * AUSD) for _ in range(2)]
    results = await asyncio.gather(
        *(world.engine.submit(world.instruction(r, amount=24 * AUSD)) for r in requests)
    )
    assert sorted(r.status for r in results) == ["blocked", "sent"]
    assert [r.reason for r in results if r.status == "blocked"] == ["over weekly cap"]
    assert (await world.budget()).spent == 84 * AUSD


async def test_signer_failure_releases_the_reservation(world: World) -> None:
    world.crypto.mode = "fail"
    request = await world.add_request(10 * AUSD)
    result = await world.engine.submit(world.instruction(request))
    assert (result.status, result.reason) == ("blocked", "signer failed: RPC")
    budget = await world.budget()
    assert (budget.spent, budget.reserved) == (0, 0)
    again = await world.engine.submit(world.instruction(request))
    assert again.status == "blocked" and len(world.crypto.calls) == 1


async def test_unknown_signer_outcome_keeps_the_reservation_until_a_retry(world: World) -> None:
    world.crypto.mode = "unavailable"
    request = await world.add_request(10 * AUSD)
    result = await world.engine.submit(world.instruction(request))
    assert result.reason == SIGNER_UNAVAILABLE
    assert (await world.budget()).reserved == 8 * AUSD

    world.crypto.mode = "confirm"
    retried = await world.engine.submit(world.instruction(request))
    assert retried.status == "sent"
    assert [c.idempotency_key for c in world.crypto.calls] == [f"req:{request.id}:1"] * 2
    budget = await world.budget()
    assert (budget.spent, budget.reserved) == (8 * AUSD, 0)


async def test_tampered_policy_blocks_every_transfer(world: World) -> None:
    """SEC-04: raising the cap in the database does not raise what the engine allows."""
    raised = POLICY.model_copy(update={"max_per_request": 90 * AUSD, "weekly_cap": 1000 * AUSD})
    async with world.sessionmaker() as session:
        await session.execute(
            update(Policy)
            .where(Policy.family_id == world.family.id)
            .values(body=raised.model_dump(mode="json"), body_hash=policy_hash(raised))
        )
        await session.commit()
    request = await world.add_request(10 * AUSD)
    result = await world.engine.submit(world.instruction(request))
    assert result.reason == "policy signature invalid"


async def test_freeze_blocks(world: World) -> None:
    """SEC-17."""
    async with world.sessionmaker() as session:
        await session.execute(
            update(Family).where(Family.id == world.family.id).values(frozen=True)
        )
        await session.commit()
    request = await world.add_request(10 * AUSD)
    assert (await world.engine.submit(world.instruction(request))).reason == "frozen by guardian"


async def test_revoked_delegation_blocks(world: World) -> None:
    """Attack demo 4: the guardian revokes in Phantom; the next transfer fails."""
    world.crypto.delegate = wallet()
    request = await world.add_request(10 * AUSD)
    result = await world.engine.submit(world.instruction(request))
    assert result.reason == "insufficient delegation"


async def test_reservoir_not_owned_by_the_guardian_is_never_spent(world: World) -> None:
    """A guardian who registers another family's delegated token account as their source gets
    nothing: the engine only spends from an account the guardian owns."""
    world.crypto.owner = wallet()
    request = await world.add_request(10 * AUSD)
    assert (await world.engine.submit(world.instruction(request))).reason == (
        "insufficient delegation"
    )


async def test_skipping_the_auditor_is_refused(world: World) -> None:
    """SEC-06 / attack demo 5: a direct call without a verdict, for an emergency."""
    request = await world.add_request(10 * AUSD, emergency_claimed=True)
    result = await world.engine.submit(world.instruction(request))
    assert result.reason == "auditor verdict required"


async def test_unknown_dependent_is_refused_without_a_record(world: World) -> None:
    result = await world.engine.submit(
        world.instruction(None, dependent_id=str(uuid.uuid4()), idempotency_key="req:x:1")
    )
    assert result.reason == "unknown dependent"


class TestProofs:
    def findings(self, total: int) -> ProofFindingsRecord:
        return ProofFindingsRecord(
            merchant="Book Shop", date="2026-10-14", total=total, matches_claim=True
        )

    async def test_one_receipt_backs_one_transfer(self, world: World) -> None:
        first_request = await world.add_request(30 * AUSD)
        first_proof = await world.add_proof(first_request)
        first = await world.engine.submit(
            world.instruction(
                first_request,
                amount=30 * AUSD,
                proof_ids=[str(first_proof.id)],
                proof_findings=self.findings(30 * AUSD),
            )
        )
        assert first.status == "sent"

        # The same photo uploaded again for a new request has the same hash.
        second_request = await world.add_request(30 * AUSD)
        second_proof = await world.add_proof(second_request)
        second = await world.engine.submit(
            world.instruction(
                second_request,
                amount=30 * AUSD,
                proof_ids=[str(second_proof.id)],
                proof_findings=self.findings(30 * AUSD),
            )
        )
        assert second.reason == "proof already used"

    async def test_proof_from_another_request_is_refused(self, world: World) -> None:
        other = await world.add_request(30 * AUSD)
        proof = await world.add_proof(other)
        request = await world.add_request(30 * AUSD)
        result = await world.engine.submit(
            world.instruction(
                request,
                amount=30 * AUSD,
                proof_ids=[str(proof.id)],
                proof_findings=self.findings(30 * AUSD),
            )
        )
        assert result.reason == "proof not for this request"


class TestGuardianOverride:
    async def ask_and_answer(
        self, world: World, request: Request, amount: int, key: SigningKey | None = None
    ) -> None:
        async with world.sessionmaker() as session:
            question = GuardianQuestion(
                family_id=world.family.id,
                request_id=request.id,
                question="45 for a school trip?",
                recommendation="approve",
                urgency="normal",
                agent_view="fits the guidance",
                dedupe_key=f"ask:{request.id}",
            )
            session.add(question)
            await session.flush()
            message = answer_question_message(
                world.family.id, question.id, GuardianDecision.APPROVE, amount, "n9"
            )
            question.status = QuestionStatus.ANSWERED
            question.decision = GuardianDecision.APPROVE
            question.approved_amount = amount
            question.answer_nonce = "n9"
            question.signature = world.sign(message, key)
            await session.commit()

    async def over_limit(self, world: World) -> tuple[Request, TransferInstruction]:
        request = await world.add_request(45 * AUSD)
        proof = await world.add_proof(request)
        t = world.instruction(
            request,
            amount=45 * AUSD,
            proof_ids=[str(proof.id)],
            proof_findings=ProofFindingsRecord(
                merchant="Museum", date="2026-10-14", total=45 * AUSD, matches_claim=True
            ),
        )
        return request, t

    async def test_signed_approval_lets_the_exact_amount_through(self, world: World) -> None:
        request, t = await self.over_limit(world)
        await self.ask_and_answer(world, request, 45 * AUSD)
        assert (await world.engine.submit(t)).status == "sent"

    async def test_forged_approval_does_not_count(self, world: World) -> None:
        """SEC-05: an approval not signed by the guardian's wallet is ignored."""
        request, t = await self.over_limit(world)
        await self.ask_and_answer(world, request, 45 * AUSD, key=SigningKey.generate())
        assert (await world.engine.submit(t)).reason == "over max per request"

    async def test_approval_of_a_different_amount_does_not_count(self, world: World) -> None:
        request, t = await self.over_limit(world)
        await self.ask_and_answer(world, request, 44 * AUSD)
        assert (await world.engine.submit(t)).reason == "over max per request"


async def test_allowance_is_sent_once_per_week(world: World) -> None:
    key = f"allowance:{world.dependent_id}:2026-W42"
    t = world.instruction(
        None, trigger="allowance", amount=30 * AUSD, category="allowance", idempotency_key=key
    )
    assert (await world.engine.submit(t)).status == "sent"
    assert (await world.engine.submit(t)).status == "duplicate"
    other_key = t.model_copy(update={"idempotency_key": f"allowance:{world.dependent_id}:x"})
    assert (await world.engine.submit(other_key)).reason == "invalid allowance"
    async with world.sessionmaker() as session:
        sent = await session.scalars(select(Transfer).where(Transfer.status == TransferStatus.SENT))
        assert len(sent.all()) == 1
