"""A signed family, an active signed policy and a fake crypto layer, for DB tests."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import base58
from nacl.signing import SigningKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.contracts.agent import TransferInstruction
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


async def build_world(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
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
