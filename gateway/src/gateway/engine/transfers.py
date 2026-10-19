"""The only code path that moves money (SPEC SEC-02).

`TransferEngine.submit` handles one `TransferInstruction` from the agent:

1. Replay: a known idempotency key returns its stored result (SEC-07).
2. Load and verify every fact, under a row lock on the family's budget week (SEC-08).
3. `check_transfer` decides. Blocked: store the decision and return it.
4. Cleared: reserve the amount, spend the verdict and proofs, store a pending transfer, commit.
5. Ask the crypto layer to sign and send (outside any lock), then settle or release.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from gateway.contracts.agent import TransferInstruction, TransferResult
from gateway.contracts.crypto import (
    CryptoClient,
    CryptoTransferRequest,
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
from gateway.core.crypto import verify_ed25519
from gateway.db import audit_log
from gateway.db.models import (
    AuditorVerdict,
    BudgetCounter,
    Family,
    GuardianQuestion,
    Policy,
    Proof,
    ReceiptFingerprint,
    Request,
    Transfer,
    UsedProofKey,
    User,
)
from gateway.engine.checks import RequestFacts, TransferFacts, VerdictFacts, check_transfer
from gateway.engine.proofs import keyed_fingerprint, proof_keys
from gateway.enums import (
    AuditEvent,
    GuardianDecision,
    PolicyStatus,
    QuestionStatus,
    TransferStatus,
    TransferTrigger,
)
from gateway.views import load_history, to_agent_proof

logger = logging.getLogger(__name__)

SIGNER_UNAVAILABLE = "signer unavailable; transfer pending"


def week_start(now: datetime, timezone: str) -> date:
    """Monday of the budget week containing `now`, in the family timezone (SPEC §5.2)."""
    local = now.astimezone(ZoneInfo(timezone))
    return local.date() - timedelta(days=local.weekday())


def allowance_key(dependent_id: str, now: datetime, timezone: str) -> str:
    """The agent layer's allowance idempotency key for the current ISO week."""
    year, week, _ = now.astimezone(ZoneInfo(timezone)).date().isocalendar()
    return f"allowance:{dependent_id}:{year}-W{week:02d}"


def usable_delegation(delegation: Delegation, agent_wallet: str, guardian_wallet: str) -> int:
    """What the agent wallet may spend: only from the guardian's own token account, and only
    while the agent wallet is its delegate. Anything else counts as nothing."""
    if delegation.owner != guardian_wallet or delegation.delegate != agent_wallet:
        return 0
    return delegation.delegated_amount_units


async def verified_approvals(
    session: AsyncSession, family_id: uuid.UUID, guardian_wallet: str, request_id: uuid.UUID
) -> list[int]:
    """Amounts the guardian approved for this request with a valid wallet signature, oldest
    first (SPEC SEC-05). What anyone says in chat never counts."""
    questions = await session.scalars(
        select(GuardianQuestion)
        .where(
            GuardianQuestion.family_id == family_id,
            GuardianQuestion.request_id == request_id,
            GuardianQuestion.status == QuestionStatus.ANSWERED,
            GuardianQuestion.decision == GuardianDecision.APPROVE,
        )
        .order_by(GuardianQuestion.answered_at)
    )
    amounts = []
    for q in questions:
        if q.signature is None or q.answer_nonce is None or q.approved_amount is None:
            continue
        message = answer_question_message(
            family_id, q.id, GuardianDecision.APPROVE, q.approved_amount, q.answer_nonce
        )
        if verify_ed25519(guardian_wallet, message, q.signature):
            amounts.append(q.approved_amount)
    return amounts


async def pending_amount(session: AsyncSession, request_id: uuid.UUID) -> int:
    """Money cleared for this request whose outcome is not known yet."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Transfer.amount), 0)).where(
            Transfer.request_id == request_id, Transfer.status == TransferStatus.PENDING
        )
    )
    return int(total or 0)


async def lock_reservoir(session: AsyncSession, token_account: str) -> None:
    """Serializes clearing transfers that draw on one reservoir, across all of the guardian's
    dependents, until the transaction ends. Taken before any budget row lock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"reservoir:{token_account}"}
    )


async def reservoir_pending(session: AsyncSession, token_account: str) -> int:
    """Money cleared from this reservoir, by any family, that has not settled yet. It is still
    inside the on-chain delegation, so it is not available to anyone else."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Transfer.amount), 0))
        .join(Family, Family.id == Transfer.family_id)
        .where(
            Family.guardian_token_account == token_account,
            Transfer.status == TransferStatus.PENDING,
        )
    )
    return int(total or 0)


def parse_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class FamilyRow:
    family: Family
    guardian_wallet: str
    dependent_wallet: str


class TransferEngine:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        crypto: CryptoClient,
        *,
        agent_wallet: str,
        mint: str,
        fingerprint_key: bytes = b"",
        camera_only: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._crypto = crypto
        self._agent_wallet = agent_wallet
        self._mint = mint
        self._camera_only = camera_only
        self._fingerprint_key = fingerprint_key
        self._clock = clock

    async def submit(self, t: TransferInstruction) -> TransferResult:
        async with self._sessionmaker() as session:
            existing = await session.scalar(
                select(Transfer).where(Transfer.idempotency_key == t.idempotency_key)
            )
        if existing is not None:
            return await self._replay(existing)

        decided = await self._decide(t)
        if isinstance(decided, TransferResult):
            return decided
        return await self._send(decided)

    # --- decide -------------------------------------------------------------------------------

    async def _decide(self, t: TransferInstruction) -> TransferResult | uuid.UUID:
        """Blocked: the stored result. Cleared: the id of the new pending transfer."""
        now = self._clock()
        async with self._sessionmaker() as session:
            found = await self._load_family(session, t.dependent_id)
            if found is None:
                # Nothing to attach a record to; the agent still gets a clear refusal.
                return TransferResult(
                    status="blocked", decision_id=str(uuid.uuid4()), reason="unknown dependent"
                )
            family = found.family
            # Network read before taking the lock, so no lock waits on the crypto layer.
            delegated_remaining = await self._delegated_remaining(found)

            await lock_reservoir(session, family.guardian_token_account)
            week = week_start(now, family.timezone)
            await session.execute(
                insert(BudgetCounter)
                .values(family_id=family.id, week_start=week)
                .on_conflict_do_nothing()
            )
            budget = await session.scalar(
                select(BudgetCounter)
                .where(BudgetCounter.family_id == family.id, BudgetCounter.week_start == week)
                .with_for_update()
            )
            assert budget is not None

            facts, verdict_row = await self._load_facts(
                session, t, found, budget, delegated_remaining, now
            )
            reason = check_transfer(facts)
            transfer = Transfer(
                family_id=family.id,
                request_id=parse_uuid(t.request_id) if facts.request else None,
                trigger=TransferTrigger(t.trigger),
                idempotency_key=t.idempotency_key,
                amount=t.amount,
                purpose=t.purpose,
                category=t.category.value,
                category_confidence=t.category_confidence,
                reasoning=t.reasoning,
                policy_version=t.policy_version,
                auditor_verdict_id=verdict_row.id if verdict_row else None,
                proof_ids=list(t.proof_ids),
                proof_findings=t.proof_findings.model_dump() if t.proof_findings else None,
                status=TransferStatus.BLOCKED if reason else TransferStatus.PENDING,
                reason=reason,
                destination_token_account=None if reason else family.dependent_token_account,
                # The engine's clock, not the database's: settling finds the reserved budget
                # week from this timestamp.
                created_at=now,
            )
            session.add(transfer)
            try:
                await session.flush()
            except IntegrityError:
                # The same key raced us; the other request owns it.
                await session.rollback()
                return await self.submit(t)

            if reason:
                await audit_log.append(
                    session,
                    family.id,
                    AuditEvent.TRANSFER_BLOCKED,
                    {"transfer_id": str(transfer.id), "amount": t.amount, "reason": reason},
                )
                await session.commit()
                return TransferResult(status="blocked", decision_id=str(transfer.id), reason=reason)

            budget.reserved += t.amount
            if verdict_row is not None and facts.verdict is not None:
                verdict_row.used = True
            for proof in facts.proofs:
                assert proof is not None
                for key in proof_keys(proof, t.proof_findings):
                    session.add(
                        UsedProofKey(
                            guardian_user_id=family.guardian_user_id,
                            key=key,
                            family_id=family.id,
                            transfer_id=transfer.id,
                        )
                    )
            if t.proof_findings is not None and t.proof_ids:
                session.add(
                    ReceiptFingerprint(
                        key_hash=keyed_fingerprint(t.proof_findings, self._fingerprint_key),
                        family_id=family.id,
                        transfer_id=transfer.id,
                    )
                )
            await audit_log.append(
                session,
                family.id,
                AuditEvent.TRANSFER_PENDING,
                {"transfer_id": str(transfer.id), "amount": t.amount},
            )
            try:
                await session.commit()
            except IntegrityError:
                # A proof key was spent by a concurrent transfer between check and commit.
                await session.rollback()
                return await self._store_blocked(t, family.id, "proof already used")
            return transfer.id

    async def _load_family(self, session: AsyncSession, dependent_id: str) -> FamilyRow | None:
        dependent_uuid = parse_uuid(dependent_id)
        if dependent_uuid is None:
            return None
        guardian, dependent = aliased(User), aliased(User)
        row = (
            await session.execute(
                select(Family, guardian.wallet_address, dependent.wallet_address)
                .join(guardian, guardian.id == Family.guardian_user_id)
                .join(dependent, dependent.id == Family.dependent_user_id)
                .where(Family.dependent_user_id == dependent_uuid)
            )
        ).first()
        if row is None:
            return None
        return FamilyRow(family=row[0], guardian_wallet=row[1], dependent_wallet=row[2])

    async def _delegated_remaining(self, found: FamilyRow) -> int | None:
        try:
            delegation = await self._crypto.get_delegation(found.family.guardian_token_account)
        except CryptoUnavailableError:
            logger.warning("Delegation read failed; treating as no delegation")
            return None
        return usable_delegation(delegation, self._agent_wallet, found.guardian_wallet)

    async def _load_facts(
        self,
        session: AsyncSession,
        t: TransferInstruction,
        found: FamilyRow,
        budget: BudgetCounter,
        delegated_remaining: int | None,
        now: datetime,
    ) -> tuple[TransferFacts, AuditorVerdict | None]:
        family = found.family
        dependent_id = str(family.dependent_user_id)

        policy_row = await session.scalar(
            select(Policy).where(
                Policy.family_id == family.id, Policy.status == PolicyStatus.ACTIVE
            )
        )
        policy, signature_valid = None, False
        if policy_row is not None:
            try:
                policy = PolicyBody.model_validate(policy_row.body)
            except ValidationError:
                policy = None
            signature_valid = policy is not None and self._policy_signature_valid(
                found, policy_row, policy
            )

        destination_registered = verify_ed25519(
            found.guardian_wallet,
            register_dependent_message(
                None,
                found.dependent_wallet,
                family.dependent_token_account,
                family.registration_nonce,
            ),
            family.registration_signature,
        )

        request_facts, request_uuid = None, parse_uuid(t.request_id)
        if request_uuid is not None:
            request = await session.scalar(
                select(Request).where(Request.id == request_uuid, Request.family_id == family.id)
            )
            if request is not None:
                request_facts = RequestFacts(
                    dependent_id=dependent_id,
                    amount_requested=request.amount_requested,
                    already_sent=request.already_sent,
                    emergency_claimed=request.emergency_claimed,
                    already_pending=await pending_amount(session, request.id),
                )

        verdict_row, verdict_facts = None, None
        verdict_uuid = parse_uuid(t.auditor_verdict_id)
        if verdict_uuid is not None:
            verdict_row = await session.scalar(
                select(AuditorVerdict).where(
                    AuditorVerdict.id == verdict_uuid, AuditorVerdict.family_id == family.id
                )
            )
            if verdict_row is not None:
                verdict_facts = VerdictFacts(
                    verdict=verdict_row.verdict.value,
                    used=verdict_row.used,
                    dependent_id=dependent_id,
                    idempotency_key=verdict_row.idempotency_key,
                    amount=verdict_row.amount,
                    category=verdict_row.category,
                )

        proofs = []
        for proof_id in t.proof_ids:
            proof_uuid = parse_uuid(proof_id)
            row = None
            if proof_uuid is not None:
                row = await session.scalar(
                    select(Proof).where(Proof.id == proof_uuid, Proof.family_id == family.id)
                )
            proofs.append(to_agent_proof(row, dependent_id, with_content=False) if row else None)

        origins = {}
        for proof in proofs:
            origin_uuid = parse_uuid(proof.derived_from) if proof else None
            if origin_uuid is not None:
                origin = await session.scalar(
                    select(Proof).where(Proof.id == origin_uuid, Proof.family_id == family.id)
                )
                if origin is not None:
                    origins[str(origin.id)] = to_agent_proof(
                        origin, dependent_id, with_content=False
                    )

        receipt_used = False
        if t.proof_findings is not None:
            receipt_used = (
                await session.scalar(
                    select(ReceiptFingerprint.key_hash).where(
                        ReceiptFingerprint.key_hash
                        == keyed_fingerprint(t.proof_findings, self._fingerprint_key)
                    )
                )
            ) is not None

        # Across all of the guardian's dependents: siblings can't reuse each other's receipts.
        used_keys = await session.scalars(
            select(UsedProofKey.key).where(UsedProofKey.guardian_user_id == family.guardian_user_id)
        )

        override = False
        if t.trigger == "request" and request_facts is not None and request_uuid is not None:
            approved = await verified_approvals(
                session, family.id, found.guardian_wallet, request_uuid
            )
            override = t.amount in approved

        facts = TransferFacts(
            instruction=t,
            family_found=True,
            frozen=family.frozen,
            policy=policy,
            policy_version=policy_row.version if policy_row else None,
            policy_signature_valid=signature_valid,
            destination_registered=destination_registered,
            spent=budget.spent,
            reserved=budget.reserved,
            reservoir_reserved=await reservoir_pending(session, family.guardian_token_account),
            delegated_remaining=delegated_remaining,
            expected_allowance_key=allowance_key(dependent_id, now, family.timezone),
            request=request_facts,
            history=await load_history(session, family.id),
            verdict=verdict_facts,
            proofs=proofs,
            used_proof_keys=frozenset(used_keys),
            camera_only=self._camera_only,
            proof_origins=origins,
            receipt_already_used=receipt_used,
            guardian_override=override,
        )
        return facts, verdict_row

    @staticmethod
    def _policy_signature_valid(found: FamilyRow, row: Policy, body: PolicyBody) -> bool:
        """The stored hash must match the body, and the guardian's signature must cover that
        hash, so a DB edit to the body, hash or version blocks transfers (SPEC SEC-04)."""
        if row.guardian_signature is None or row.activation_nonce is None:
            return False
        if policy_hash(body) != row.body_hash:
            return False
        message = activate_policy_message(
            found.family.id, row.version, row.body_hash, row.activation_nonce
        )
        return verify_ed25519(found.guardian_wallet, message, row.guardian_signature)

    async def _store_blocked(
        self, t: TransferInstruction, family_id: uuid.UUID, reason: str
    ) -> TransferResult:
        async with self._sessionmaker() as session:
            transfer = Transfer(
                family_id=family_id,
                trigger=TransferTrigger(t.trigger),
                idempotency_key=t.idempotency_key,
                amount=t.amount,
                purpose=t.purpose,
                category=t.category.value,
                category_confidence=t.category_confidence,
                reasoning=t.reasoning,
                policy_version=t.policy_version,
                proof_ids=list(t.proof_ids),
                status=TransferStatus.BLOCKED,
                reason=reason,
            )
            session.add(transfer)
            await audit_log.append(
                session,
                family_id,
                AuditEvent.TRANSFER_BLOCKED,
                {"transfer_id": str(transfer.id), "amount": t.amount, "reason": reason},
            )
            await session.commit()
            return TransferResult(status="blocked", decision_id=str(transfer.id), reason=reason)

    # --- send and settle ----------------------------------------------------------------------

    async def retry_pending(self) -> list[tuple[uuid.UUID, TransferResult]]:
        """Finishes every pending transfer by its own idempotency key; the crypto layer never
        signs a key twice. Returns (family id, result) per transfer."""
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Transfer.id, Transfer.family_id)
                    .where(Transfer.status == TransferStatus.PENDING)
                    .order_by(Transfer.created_at)
                )
            ).all()
        return [(family_id, await self._send(transfer_id)) for transfer_id, family_id in rows]

    async def _replay(self, existing: Transfer) -> TransferResult:
        if existing.status == TransferStatus.SENT:
            return TransferResult(
                status="duplicate",
                decision_id=str(existing.id),
                amount=existing.amount,
                tx_signature=existing.tx_signature,
            )
        if existing.status == TransferStatus.PENDING:
            # A crash or timeout left it unfinished. The crypto layer is idempotent on the
            # key, so asking again can never sign twice.
            return await self._send(existing.id)
        return TransferResult(
            status="blocked", decision_id=str(existing.id), reason=existing.reason
        )

    async def _send(self, transfer_id: uuid.UUID) -> TransferResult:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(Transfer, Family)
                    .join(Family, Family.id == Transfer.family_id)
                    .where(Transfer.id == transfer_id)
                )
            ).one()
        transfer, family = row[0], row[1]
        assert transfer.destination_token_account is not None
        request = CryptoTransferRequest(
            transfer_id=transfer.id,
            family_id=family.id,
            amount_units=transfer.amount,
            source_token_account=family.guardian_token_account,
            destination_token_account=transfer.destination_token_account,
            mint=self._mint,
            idempotency_key=transfer.idempotency_key,
        )
        try:
            response = await self._crypto.transfer(request)
        except CryptoUnavailableError:
            # Outcome unknown: it may still go out, so it is never reported as blocked. The
            # reservation and the pending row stay; the retry job (or a replay) finishes it.
            logger.warning("Crypto layer unavailable for transfer %s", transfer.id)
            return TransferResult(
                status="pending",
                decision_id=str(transfer.id),
                amount=transfer.amount,
                reason=SIGNER_UNAVAILABLE,
            )
        confirmed = response.status == CryptoTransferStatus.CONFIRMED and response.tx_signature
        return await self._settle(
            transfer.id,
            tx_signature=response.tx_signature if confirmed else None,
            error_code=None if confirmed else (response.error_code or "unknown"),
        )

    async def _settle(
        self, transfer_id: uuid.UUID, tx_signature: str | None, error_code: str | None
    ) -> TransferResult:
        async with self._sessionmaker() as session:
            transfer = await session.scalar(
                select(Transfer).where(Transfer.id == transfer_id).with_for_update()
            )
            assert transfer is not None
            if transfer.status != TransferStatus.PENDING:
                # Settled by a concurrent replay.
                return await self._replay(transfer)
            family = await session.scalar(select(Family).where(Family.id == transfer.family_id))
            assert family is not None
            budget = await session.scalar(
                select(BudgetCounter)
                .where(
                    BudgetCounter.family_id == family.id,
                    BudgetCounter.week_start == week_start(transfer.created_at, family.timezone),
                )
                .with_for_update()
            )
            assert budget is not None
            budget.reserved -= transfer.amount

            if tx_signature is None:
                transfer.status = TransferStatus.FAILED
                transfer.reason = f"signer failed: {error_code}"
                event = AuditEvent.TRANSFER_FAILED
            else:
                budget.spent += transfer.amount
                transfer.status = TransferStatus.SENT
                transfer.tx_signature = tx_signature
                event = AuditEvent.TRANSFER_SENT
                if transfer.request_id is not None:
                    request = await session.scalar(
                        select(Request).where(Request.id == transfer.request_id).with_for_update()
                    )
                    assert request is not None
                    request.already_sent += transfer.amount

            await audit_log.append(
                session,
                family.id,
                event,
                {
                    "transfer_id": str(transfer.id),
                    "amount": transfer.amount,
                    "tx_signature": tx_signature,
                    "error_code": error_code,
                },
            )
            await session.commit()

            if tx_signature is None:
                return TransferResult(
                    status="blocked", decision_id=str(transfer.id), reason=transfer.reason
                )
            return TransferResult(
                status="sent",
                decision_id=str(transfer.id),
                amount=transfer.amount,
                tx_signature=tx_signature,
            )
