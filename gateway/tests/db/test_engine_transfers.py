import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import base58
import pytest
from nacl.signing import SigningKey
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.contracts.agent import ProofFindingsRecord, TransferInstruction
from gateway.contracts.crypto import (
    CryptoTransferRequest,
    CryptoTransferResponse,
    CryptoTransferStatus,
    CryptoUnavailableError,
    Delegation,
)
from gateway.contracts.policy import PolicyBody, policy_hash
from gateway.contracts.signing import (
    activate_policy_message,
    answer_question_message,
    register_dependent_message,
)
from gateway.db import audit_log
from gateway.db.models import (
    BudgetCounter,
    Family,
    GuardianQuestion,
    Policy,
    Proof,
    Request,
    Transfer,
    User,
)
from gateway.engine.transfers import SIGNER_UNAVAILABLE, TransferEngine
from gateway.enums import (
    GuardianDecision,
    PolicyStatus,
    QuestionStatus,
    Role,
    TransferStatus,
)

AUSD = 1_000_000
NOW = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)  # Wednesday, ISO week 42 in Bangkok
WEEK = date(2026, 10, 12)


def wallet(key: SigningKey | None = None) -> str:
    key = key or SigningKey.generate()
    return base58.b58encode(bytes(key.verify_key)).decode()


AGENT_WALLET = wallet()
MINT = wallet()

POLICY = PolicyBody(
    weekly_cap=100 * AUSD,
    max_per_request=40 * AUSD,
    blocked_categories=["gaming"],
    proof_threshold=25 * AUSD,
    review_threshold=40 * AUSD,  # keeps Auditor review out of the way in most tests
    allowance={"amount": 30 * AUSD, "weekday": 0},
)


@dataclass
class FakeCrypto:
    delegate: str = AGENT_WALLET
    delegated: int = 500 * AUSD
    mode: str = "confirm"  # confirm | fail | unavailable
    calls: list[CryptoTransferRequest] = field(default_factory=list)
    signatures: dict[str, str] = field(default_factory=dict)

    async def get_delegation(self, token_account: str) -> Delegation:
        return Delegation(
            token_account=token_account,
            delegate=self.delegate,
            delegated_amount_units=self.delegated,
        )

    async def transfer(self, request: CryptoTransferRequest) -> CryptoTransferResponse:
        self.calls.append(request)
        if self.mode == "unavailable":
            raise CryptoUnavailableError("down")
        if self.mode == "fail":
            return CryptoTransferResponse(status=CryptoTransferStatus.FAILED, error_code="RPC")
        # Idempotent on the key, like the real signer.
        signature = self.signatures.setdefault(
            request.idempotency_key, f"tx_{len(self.signatures)}"
        )
        return CryptoTransferResponse(status=CryptoTransferStatus.CONFIRMED, tx_signature=signature)


@dataclass
class World:
    sessionmaker: async_sessionmaker[AsyncSession]
    crypto: FakeCrypto
    engine: TransferEngine
    guardian_key: SigningKey
    family: Family

    @property
    def dependent_id(self) -> str:
        return str(self.family.dependent_user_id)

    def sign(self, message: str, key: SigningKey | None = None) -> str:
        return base58.b58encode(
            (key or self.guardian_key).sign(message.encode()).signature
        ).decode()

    async def add_request(self, amount_requested: int, **values: Any) -> Request:
        async with self.sessionmaker() as session:
            request = Request(
                family_id=self.family.id,
                message="please",
                amount_requested=amount_requested,
                **values,
            )
            session.add(request)
            await session.commit()
            return request

    async def add_proof(self, request: Request, content: bytes = b"receipt-photo") -> Proof:
        async with self.sessionmaker() as session:
            proof = Proof(
                request_id=request.id,
                family_id=self.family.id,
                kind="receipt",
                media_type="image/jpeg",
                size_bytes=len(content),
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                source="photo",
            )
            session.add(proof)
            await session.commit()
            return proof

    def instruction(self, request: Request | None, **overrides: Any) -> TransferInstruction:
        values: dict[str, Any] = {
            "dependent_id": self.dependent_id,
            "request_id": str(request.id) if request else None,
            "trigger": "request",
            "amount": 8 * AUSD,
            "purpose": "notebooks",
            "category": "education",
            "category_confidence": 0.9,
            "reasoning": "fits the guidance",
            "idempotency_key": f"req:{request.id if request else 'x'}:1",
            "policy_version": 1,
        }
        values.update(overrides)
        return TransferInstruction.model_validate(values)

    async def budget(self) -> BudgetCounter:
        async with self.sessionmaker() as session:
            row = await session.scalar(
                select(BudgetCounter).where(BudgetCounter.family_id == self.family.id)
            )
            assert row is not None
            return row


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    guardian_key = SigningKey.generate()
    guardian_wallet, dependent_wallet, dependent_account = wallet(guardian_key), wallet(), wallet()
    async with sessionmaker() as session:
        guardian = User(wallet_address=guardian_wallet, role=Role.GUARDIAN)
        dependent = User(wallet_address=dependent_wallet, role=Role.DEPENDENT)
        session.add_all([guardian, dependent])
        await session.flush()
        registration = register_dependent_message(None, dependent_wallet, dependent_account, "n0")
        family = Family(
            guardian_user_id=guardian.id,
            dependent_user_id=dependent.id,
            dependent_token_account=dependent_account,
            guardian_token_account=wallet(),
            registration_signature=base58.b58encode(
                guardian_key.sign(registration.encode()).signature
            ).decode(),
            registration_nonce="n0",
        )
        session.add(family)
        await session.flush()
        body_hash = policy_hash(POLICY)
        activation = activate_policy_message(family.id, 1, body_hash, "n1")
        session.add(
            Policy(
                family_id=family.id,
                version=1,
                body=POLICY.model_dump(mode="json"),
                body_hash=body_hash,
                guardian_signature=base58.b58encode(
                    guardian_key.sign(activation.encode()).signature
                ).decode(),
                activation_nonce="n1",
                status=PolicyStatus.ACTIVE,
            )
        )
        await session.commit()

    crypto = FakeCrypto()
    engine = TransferEngine(
        sessionmaker, crypto, agent_wallet=AGENT_WALLET, mint=MINT, clock=lambda: NOW
    )
    return World(sessionmaker, crypto, engine, guardian_key, family)


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
