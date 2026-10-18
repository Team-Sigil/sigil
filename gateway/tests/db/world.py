"""A signed family, an active signed policy and a fake crypto layer, for DB tests."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import base58
from nacl.signing import SigningKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.agent_client import (
    AgentDecision,
    AgentUnavailableError,
    ChatTurn,
    InterviewProgress,
    InterviewTurn,
)
from gateway.contracts.agent import (
    Category,
    PolicyDraft,
    PolicyDraftValidation,
    TestScenario,
    TransferInstruction,
)
from gateway.contracts.crypto import (
    CryptoTransferRequest,
    CryptoTransferResponse,
    CryptoTransferStatus,
    CryptoUnavailableError,
    Delegation,
)
from gateway.contracts.policy import PolicyBody, policy_hash
from gateway.contracts.signing import activate_policy_message, register_dependent_message
from gateway.db.models import BudgetCounter, Family, Policy, Proof, Request, User
from gateway.engine.transfers import TransferEngine
from gateway.enums import PolicyStatus, Role

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
    owner: str = ""  # set to the guardian's wallet by build_world
    delegate: str = AGENT_WALLET
    delegated: int = 500 * AUSD
    mode: str = "confirm"  # confirm | fail | unavailable
    calls: list[CryptoTransferRequest] = field(default_factory=list)
    signatures: dict[str, str] = field(default_factory=dict)

    async def get_delegation(self, token_account: str) -> Delegation:
        return Delegation(
            token_account=token_account,
            owner=self.owner,
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


DEMO_DRAFT = PolicyDraft(
    weekly_cap=100,
    max_per_request=40,
    blocked_categories=[Category.GAMING],
    proof_threshold=25,
    allowance_amount=30,
    allowance_weekday=0,
    guidance="School supplies and bus fare are always fine. Ask me about trips.",
    test_scenarios=[TestScenario(scenario="12.00 for a novel", what_i_would_do="Send it.")],
)


def interview_reply(
    status: str = "needs_input",
    kind: str | None = "question",
    question: str | None = "Who is it for, and roughly how much each week?",
    draft: PolicyDraft | None = None,
    phase: str = "gathering",
) -> InterviewTurn:
    """One scripted reply of the agent's policy interview."""
    return InterviewTurn.model_validate(
        {
            "protocol": "sigil.policy-interview/1",
            "status": status,
            "phase": phase,
            "question": question if status == "needs_input" else None,
            "question_kind": kind if status == "needs_input" else None,
            "checklist": [],
            "progress": InterviewProgress(required_total=8, required_done=0),
            "draft": draft,
            "validation": PolicyDraftValidation(valid=True, errors=[]) if draft else None,
        }
    )


READY = interview_reply("ready", draft=DEMO_DRAFT, phase="ready")


@dataclass
class FakeAgent:
    """Stands in for the agent service: scripted interview replies and decisions, calls
    recorded."""

    interview: list[InterviewTurn] = field(default_factory=lambda: [READY])
    outcome: str = "asked_dependent"
    reply: str = "What is it for?"
    processed: list[str] = field(default_factory=list)
    conversations: list[list[ChatTurn]] = field(default_factory=list)
    fail: bool = False

    async def process_request(self, request_id: str) -> AgentDecision:
        if self.fail:
            raise AgentUnavailableError()
        self.processed.append(request_id)
        return AgentDecision(
            dependent_id="d",
            request_id=request_id,
            outcome=self.outcome,
            reply_to_dependent=self.reply,
            summary_for_guardian="Asked what it is for.",
            amount_sent=0,
            policy_version=1,
        )

    async def interview_turn(
        self,
        guardian_id: str,
        dependent_id: str,
        conversation: list[ChatTurn],
        locale: str | None,
    ) -> InterviewTurn:
        self.conversations.append(conversation)
        return self.interview.pop(0)

    async def interview_protocol(self, locale: str | None) -> dict[str, Any]:
        return {"protocol": "sigil.policy-interview/1", "locale": locale, "max_turns": 50}


@dataclass
class World:
    sessionmaker: async_sessionmaker[AsyncSession]
    crypto: FakeCrypto
    agent: FakeAgent
    engine: TransferEngine
    guardian_key: SigningKey
    dependent_key: SigningKey
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

    async def add_proof(
        self,
        request: Request,
        content: bytes = b"receipt-photo",
        *,
        source: str = "photo",
        media_type: str = "image/jpeg",
        derived_from: Proof | None = None,
    ) -> Proof:
        async with self.sessionmaker() as session:
            proof = Proof(
                request_id=request.id,
                family_id=self.family.id,
                kind="receipt",
                media_type=media_type,
                size_bytes=len(content),
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                source=source,
                derived_from=derived_from.id if derived_from else None,
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


async def build_world(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    guardian_key, dependent_key = SigningKey.generate(), SigningKey.generate()
    guardian_wallet, dependent_wallet = wallet(guardian_key), wallet(dependent_key)
    dependent_account = wallet()
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

    crypto = FakeCrypto(owner=guardian_wallet)
    engine = TransferEngine(
        sessionmaker, crypto, agent_wallet=AGENT_WALLET, mint=MINT, clock=lambda: NOW
    )
    return World(sessionmaker, crypto, FakeAgent(), engine, guardian_key, dependent_key, family)
