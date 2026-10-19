"""What the platform API reads and writes for the agent layer. Payloads are the agent's
contract objects (`gateway.contracts.agent`); ids are our user, request and proof UUIDs.

The agent identifies a family by the guardian's user id (its `family_id` for usage) and a
dependent by the dependent's user id.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from gateway.contracts import agent
from gateway.contracts.crypto import CryptoClient, CryptoUnavailableError
from gateway.contracts.policy import PolicyBody, body_from_draft, policy_view
from gateway.core.errors import ConflictError, DomainError, NotFoundError
from gateway.db import audit_log
from gateway.db.models import (
    AgentEvent,
    AuditorVerdict,
    BudgetCounter,
    Decision,
    Family,
    GuardianQuestion,
    LlmUsage,
    Message,
    Notification,
    Policy,
    PolicyProposal,
    Proof,
    ProofObligation,
    ProofRequest,
    ReceiptFingerprint,
    Request,
    Transfer,
    UsedProofKey,
    User,
)
from gateway.engine.proofs import counts_as_proof, keyed_fingerprint, proof_keys
from gateway.engine.transfers import (
    lock_reservoir,
    owed_since,
    parse_uuid,
    pending_amount,
    reservoir_pending,
    usable_delegation,
    verified_approvals,
    week_start,
)
from gateway.enums import (
    Audience,
    AuditEvent,
    GuardianDecision,
    ObligationStatus,
    PolicyStatus,
    QuestionStatus,
    Role,
    ThreadRole,
    TransferStatus,
    TransferTrigger,
    Verdict,
)
from gateway.events import EventBus, EventType
from gateway.views import as_category, load_history, to_agent_proof

logger = logging.getLogger(__name__)


class SettleObligation(BaseModel):
    """What the agent read from the proof it says settles an owed receipt."""

    proof_id: str
    findings: agent.ProofFindingsRecord


class SettledObligation(BaseModel):
    settled: bool
    reason: str | None = None


THREAD_LIMIT = 100
DEFAULT_DEPENDENT_NAME = "your dependent"


@dataclass(frozen=True)
class Members:
    family: Family
    guardian: User
    dependent: User


async def _members(session: AsyncSession, *conditions: object) -> Members:
    guardian, dependent = aliased(User), aliased(User)
    row = (
        await session.execute(
            select(Family, guardian, dependent)
            .join(guardian, guardian.id == Family.guardian_user_id)
            .join(dependent, dependent.id == Family.dependent_user_id)
            .where(*conditions)  # type: ignore[arg-type]
        )
    ).first()
    if row is None:
        raise NotFoundError("No such family member.")
    return Members(family=row[0], guardian=row[1], dependent=row[2])


async def members_by_dependent(session: AsyncSession, dependent_id: str) -> Members:
    dependent_uuid = parse_uuid(dependent_id)
    if dependent_uuid is None:
        raise NotFoundError("No such dependent.")
    return await _members(session, Family.dependent_user_id == dependent_uuid)


async def guardian_families(session: AsyncSession, guardian_id: str) -> tuple[User, list[Family]]:
    """A guardian and all of their families (one per dependent), oldest first."""
    guardian_uuid = parse_uuid(guardian_id)
    guardian = await session.get(User, guardian_uuid) if guardian_uuid else None
    families = (
        list(
            await session.scalars(
                select(Family)
                .where(Family.guardian_user_id == guardian_uuid)
                .order_by(Family.created_at, Family.id)
            )
        )
        if guardian
        else []
    )
    if guardian is None or not families:
        raise NotFoundError("No such guardian.")
    return guardian, families


async def members_by_both(session: AsyncSession, guardian_id: str, dependent_id: str) -> Members:
    members = await members_by_dependent(session, dependent_id)
    if str(members.guardian.id) != guardian_id:
        raise NotFoundError("Guardian and dependent are not one family.")
    return members


async def _family_request(
    session: AsyncSession, family: Family, request_id: str | None
) -> Request | None:
    """The request, which must belong to the family; None when no id was given."""
    if request_id is None:
        return None
    request_uuid = parse_uuid(request_id)
    request = None
    if request_uuid is not None:
        request = await session.scalar(
            select(Request).where(Request.id == request_uuid, Request.family_id == family.id)
        )
    if request is None:
        raise NotFoundError("No such request.")
    return request


# --- reads ------------------------------------------------------------------------------------


async def list_dependents(session: AsyncSession) -> list[agent.DependentSummary]:
    families = await session.scalars(select(Family).order_by(Family.created_at))
    return [
        agent.DependentSummary(
            dependent_id=str(f.dependent_user_id), guardian_id=str(f.guardian_user_id)
        )
        for f in families
    ]


async def get_context(
    session: AsyncSession,
    crypto: CryptoClient,
    agent_wallet: str,
    dependent_id: str,
    now: datetime,
) -> agent.AgentContext:
    members = await members_by_dependent(session, dependent_id)
    family = members.family
    policy_row = await session.scalar(
        select(Policy).where(Policy.family_id == family.id, Policy.status == PolicyStatus.ACTIVE)
    )
    if policy_row is None:
        raise ConflictError("The family has no active policy.")
    body = PolicyBody.model_validate(policy_row.body)

    week = week_start(now, family.timezone)
    budget = await session.scalar(
        select(BudgetCounter).where(
            BudgetCounter.family_id == family.id, BudgetCounter.week_start == week
        )
    )
    # Reserved money is as good as spent for the agent's planning.
    spent = (budget.spent + budget.reserved) if budget else 0
    # The reservoir is shared by all of the guardian's dependents.
    reserved = await reservoir_pending(session, family.guardian_token_account)

    try:
        delegation = await crypto.get_delegation(family.guardian_token_account)
        delegated = usable_delegation(delegation, agent_wallet, members.guardian.wallet_address)
    except CryptoUnavailableError:
        logger.warning("Delegation read failed for the agent context; reporting 0")
        delegated = 0

    return agent.AgentContext(
        dependent_id=str(members.dependent.id),
        guardian_id=str(members.guardian.id),
        dependent_name=members.dependent.display_name or DEFAULT_DEPENDENT_NAME,
        policy=policy_view(body, policy_row.version, policy_row.body_hash),
        budget=agent.Budget(
            week_start=week,
            spent_this_week=spent,
            remaining_this_week=max(0, body.weekly_cap - spent),
        ),
        delegated_amount=max(0, delegated - reserved),
        frozen=family.frozen,
        history=await load_history(session, family.id),
        dependent_locale=members.dependent.locale,
        guardian_locale=members.guardian.locale,
    )


async def weekly_activity(
    session: AsyncSession, dependent_id: str, now: datetime
) -> agent.WeeklyActivity:
    members = await members_by_dependent(session, dependent_id)
    family = members.family
    week = week_start(now, family.timezone)
    zone = ZoneInfo(family.timezone)
    policy_row = await session.scalar(
        select(Policy).where(Policy.family_id == family.id, Policy.status == PolicyStatus.ACTIVE)
    )
    cap = PolicyBody.model_validate(policy_row.body).weekly_cap if policy_row else 0
    items = [
        item
        for item in await load_history(session, family.id)
        if item.at.astimezone(zone).date() >= week
    ]
    return agent.WeeklyActivity(
        dependent_id=dependent_id, week_start=week, items=items, weekly_cap=cap
    )


async def get_request(session: AsyncSession, request_id: str) -> agent.DependentRequest:
    request_uuid = parse_uuid(request_id)
    request = None
    if request_uuid is not None:
        request = await session.scalar(select(Request).where(Request.id == request_uuid))
    if request is None:
        raise NotFoundError("No such request.")
    family = await session.scalar(select(Family).where(Family.id == request.family_id))
    assert family is not None
    guardian = await session.get(User, family.guardian_user_id)
    assert guardian is not None
    # The latest verified approval: exactly this amount may pass the per-request max, the
    # weekly cap and review. The engine re-verifies the signature before any money moves.
    approvals = await verified_approvals(session, family.id, guardian.wallet_address, request.id)

    messages = list(
        await session.scalars(
            select(Message)
            .where(Message.request_id == request.id)
            .order_by(Message.created_at.desc())
            .limit(THREAD_LIMIT)
        )
    )
    listed = select(Proof).where(Proof.request_id == request.id).order_by(Proof.uploaded_at)
    # While a receipt is owed, the agent reads the proofs sent for it, not the ones that came
    # before the money went out (it reads at most 3).
    opened = await owed_since(session, request.id)
    if opened is not None:
        listed = listed.where(Proof.uploaded_at >= opened)
    proofs = await session.scalars(listed)
    return agent.DependentRequest(
        id=str(request.id),
        dependent_id=str(family.dependent_user_id),
        message=request.message,
        amount_requested=request.amount_requested,
        emergency_claimed=request.emergency_claimed,
        already_sent=request.already_sent,
        already_pending=await pending_amount(session, request.id),
        guardian_approved_amount=approvals[-1] if approvals else None,
        thread=[
            agent.ThreadMessage(role=m.role.value, text=m.text, at=m.created_at)
            for m in reversed(messages)
        ],
        proofs=[agent.ProofRef(id=str(p.id), media_type=p.media_type, kind=p.kind) for p in proofs],
    )


async def get_proof(session: AsyncSession, proof_id: str) -> agent.Proof:
    proof_uuid = parse_uuid(proof_id)
    row = None
    if proof_uuid is not None:
        row = await session.scalar(select(Proof).where(Proof.id == proof_uuid))
    if row is None:
        raise NotFoundError("No such proof.")
    family = await session.scalar(select(Family).where(Family.id == row.family_id))
    assert family is not None
    return to_agent_proof(row, str(family.dependent_user_id), with_content=True)


async def guardian_answers(session: AsyncSession, guardian_id: str) -> list[agent.GuardianAnswer]:
    """Answers across all of the guardian's dependents; each names its dependent."""
    _, families = await guardian_families(session, guardian_id)
    dependent_of = {f.id: str(f.dependent_user_id) for f in families}
    rows = await session.execute(
        select(GuardianQuestion, Request.amount_requested)
        .outerjoin(Request, Request.id == GuardianQuestion.request_id)
        .where(
            GuardianQuestion.family_id.in_(dependent_of),
            GuardianQuestion.status == QuestionStatus.ANSWERED,
        )
        .order_by(GuardianQuestion.answered_at)
    )
    answers = []
    for question, amount_requested in rows:
        approved = question.decision == GuardianDecision.APPROVE
        amount = question.approved_amount or 0
        partial = approved and amount_requested is not None and amount < amount_requested
        answers.append(
            agent.GuardianAnswer(
                question_id=str(question.id),
                dependent_id=dependent_of[question.family_id],
                category=as_category(question.category or ""),
                answer="partial" if partial else "approved" if approved else "declined",
                amount=amount if approved else 0,
                at=question.answered_at or question.created_at,
            )
        )
    return answers


async def usage_status(
    session: AsyncSession, guardian_id: str, daily_budget: int, now: datetime
) -> agent.UsageStatus:
    guardian, families = await guardian_families(session, guardian_id)
    # The usage "family" is the guardian; their first family's timezone sets the day.
    day = now.astimezone(ZoneInfo(families[0].timezone)).date()
    used = await session.scalar(
        select(LlmUsage.tokens_used).where(
            LlmUsage.guardian_user_id == guardian.id, LlmUsage.day == day
        )
    )
    return agent.UsageStatus(remaining_tokens=daily_budget - (used or 0))


# --- writes -----------------------------------------------------------------------------------


async def report_usage(
    session: AsyncSession, report: agent.UsageReport, daily_budget: int, now: datetime
) -> agent.UsageStatus:
    guardian, families = await guardian_families(session, report.family_id)
    day = now.astimezone(ZoneInfo(families[0].timezone)).date()
    tokens = report.input_tokens + report.output_tokens
    stmt = insert(LlmUsage).values(guardian_user_id=guardian.id, day=day, tokens_used=tokens)
    used = await session.scalar(
        stmt.on_conflict_do_update(
            index_elements=[LlmUsage.guardian_user_id, LlmUsage.day],
            set_={"tokens_used": LlmUsage.tokens_used + tokens},
        ).returning(LlmUsage.tokens_used)
    )
    await session.commit()
    return agent.UsageStatus(remaining_tokens=daily_budget - (used or 0))


async def record_verdict(
    session: AsyncSession, submission: agent.VerdictSubmission
) -> agent.VerdictReceipt:
    members = await members_by_dependent(session, submission.dependent_id)
    request = await _family_request(session, members.family, submission.request_id)
    verdict = AuditorVerdict(
        family_id=members.family.id,
        request_id=request.id if request else None,
        idempotency_key=submission.idempotency_key,
        amount=submission.amount,
        category=submission.category.value,
        verdict=Verdict(submission.verdict),
        reason=submission.reason,
        concerns=submission.concerns,
        independent_assessment=submission.independent_assessment.model_dump(),
        review_triggers=submission.review_triggers,
        model=submission.model,
    )
    session.add(verdict)
    await session.flush()
    await audit_log.append(
        session,
        members.family.id,
        AuditEvent.VERDICT_RECORDED,
        {
            "verdict_id": str(verdict.id),
            "verdict": submission.verdict,
            "idempotency_key": submission.idempotency_key,
            "amount": submission.amount,
        },
    )
    await session.commit()
    return agent.VerdictReceipt(verdict_id=str(verdict.id))


async def ask_guardian(
    session: AsyncSession, q: agent.GuardianQuestion, bus: EventBus
) -> agent.Receipt:
    members = await members_by_both(session, q.guardian_id, q.dependent_id)
    existing = await session.scalar(
        select(GuardianQuestion.id).where(GuardianQuestion.dedupe_key == q.dedupe_key)
    )
    if existing is not None:
        return agent.Receipt(id=str(existing))
    request = await _family_request(session, members.family, q.request_id)
    # The category the agent was about to pay for, so answers can teach it per category.
    category = None
    if request is not None:
        category = await session.scalar(
            select(Transfer.category)
            .where(Transfer.request_id == request.id)
            .order_by(Transfer.created_at.desc())
            .limit(1)
        )
    question = GuardianQuestion(
        family_id=members.family.id,
        request_id=request.id if request else None,
        question=q.question,
        recommendation=q.recommendation,
        recommended_amount=q.recommended_amount,
        urgency=q.urgency,
        category=category,
        agent_view=q.agent_view,
        auditor_view=q.auditor_view,
        dedupe_key=q.dedupe_key,
    )
    session.add(question)
    await session.flush()
    await audit_log.append(
        session, members.family.id, AuditEvent.QUESTION_ASKED, {"question_id": str(question.id)}
    )
    await session.commit()
    bus.publish(
        members.family.id, EventType.QUESTION, {"question_id": str(question.id)}, Role.GUARDIAN
    )
    return agent.Receipt(id=str(question.id))


async def request_proof(session: AsyncSession, req: agent.ProofRequest) -> agent.Receipt:
    members = await members_by_dependent(session, req.dependent_id)
    request = await _family_request(session, members.family, req.request_id)
    assert request is not None
    row = ProofRequest(request_id=request.id, kind=req.kind, reason=req.reason)
    session.add(row)
    await session.commit()
    return agent.Receipt(id=str(row.id))


async def propose_policy_update(
    session: AsyncSession, proposal: agent.PolicyProposal
) -> agent.Receipt:
    members = await members_by_both(session, proposal.guardian_id, proposal.dependent_id)
    existing = await session.scalar(
        select(PolicyProposal.id).where(PolicyProposal.dedupe_key == proposal.dedupe_key)
    )
    if existing is not None:
        return agent.Receipt(id=str(existing))
    row = PolicyProposal(
        family_id=members.family.id,
        change=proposal.change,
        evidence=proposal.evidence,
        dedupe_key=proposal.dedupe_key,
    )
    session.add(row)
    await session.commit()
    return agent.Receipt(id=str(row.id))


async def notify(session: AsyncSession, note: agent.Notification, bus: EventBus) -> agent.Receipt:
    audience = Audience(note.audience)
    if audience == Audience.GUARDIAN and note.dependent_id is not None:
        # A guardian note about one dependent (the weekly summary, the maker's note): filed
        # under that dependent's family, which must be this guardian's.
        members = await members_by_both(session, note.recipient_id, note.dependent_id)
        recipient, families, family_id = members.guardian, [members.family], members.family.id
    elif audience == Audience.GUARDIAN:
        # About all of the guardian's dependents (e.g. "top up the shared reservoir").
        recipient, families = await guardian_families(session, note.recipient_id)
        family_id = None
    elif note.dependent_id is not None:
        raise DomainError("dependent_id is only for notes to a guardian.")
    else:
        members = await members_by_dependent(session, note.recipient_id)
        recipient, families, family_id = members.dependent, [members.family], members.family.id
    if note.dedupe_key is not None:
        existing = await session.scalar(
            select(Notification.id).where(Notification.dedupe_key == note.dedupe_key)
        )
        if existing is not None:
            return agent.Receipt(id=str(existing))
    row = Notification(
        family_id=family_id,
        audience=audience,
        recipient_id=recipient.id,
        message=note.message,
        dedupe_key=note.dedupe_key,
    )
    session.add(row)
    await session.commit()
    for family in families:
        bus.publish(
            family.id,
            EventType.NOTIFICATION,
            {"notification_id": str(row.id)},
            Role.GUARDIAN if audience == Audience.GUARDIAN else Role.DEPENDENT,
        )
    return agent.Receipt(id=str(row.id))


async def _run_transfer(
    session: AsyncSession, family: Family, request: Request | None
) -> uuid.UUID | None:
    """The transfer the run being recorded produced: the latest sent or pending transfer for
    the same request (or the family's allowance) since the previous decision on it."""
    same_subject = (
        Transfer.request_id == request.id
        if request is not None
        else (Transfer.request_id.is_(None)) & (Transfer.trigger == TransferTrigger.ALLOWANCE)
    )
    previous = await session.scalar(
        select(func.max(Decision.created_at)).where(
            Decision.family_id == family.id,
            Decision.request_id == request.id
            if request is not None
            else Decision.request_id.is_(None),
        )
    )
    conditions = [
        Transfer.family_id == family.id,
        same_subject,
        Transfer.status.in_([TransferStatus.SENT, TransferStatus.PENDING]),
    ]
    if previous is not None:
        conditions.append(Transfer.created_at > previous)
    transfer_id: uuid.UUID | None = await session.scalar(
        select(Transfer.id).where(*conditions).order_by(Transfer.created_at.desc()).limit(1)
    )
    return transfer_id


async def record_decision(
    session: AsyncSession, record: agent.DecisionRecord, bus: EventBus
) -> agent.Receipt:
    members = await members_by_dependent(session, record.dependent_id)
    request = await _family_request(session, members.family, record.request_id)
    transfer_id = await _run_transfer(session, members.family, request)
    decision = Decision(
        family_id=members.family.id,
        request_id=request.id if request else None,
        outcome=record.outcome.value,
        reply_to_dependent=record.reply_to_dependent,
        summary_for_guardian=record.summary_for_guardian,
        amount_sent=record.amount_sent,
        policy_version=record.policy_version,
        guardrail_events=record.guardrail_events,
        transfer_id=transfer_id,
    )
    session.add(decision)
    if request is not None:
        # The reply joins the conversation, so the next run sees what the agent said.
        session.add(
            Message(request_id=request.id, role=ThreadRole.AGENT, text=record.reply_to_dependent)
        )
    await session.flush()
    await audit_log.append(
        session,
        members.family.id,
        AuditEvent.DECISION_RECORDED,
        {
            "decision_id": str(decision.id),
            "outcome": record.outcome.value,
            "amount_sent": record.amount_sent,
            "transfer_id": str(transfer_id) if transfer_id else None,
        },
    )
    await session.commit()
    if request is not None:
        bus.publish(members.family.id, EventType.MESSAGE, {"request_id": str(request.id)})
    return agent.Receipt(id=str(decision.id))


async def log_event(session: AsyncSession, event: agent.AgentEvent) -> agent.Receipt:
    row = AgentEvent(kind=event.kind, subject_id=event.subject_id, detail=event.detail)
    session.add(row)
    await session.commit()
    return agent.Receipt(id=str(row.id))


async def validate_policy_draft(
    session: AsyncSession, submission: agent.PolicyDraftSubmission
) -> agent.PolicyDraftValidation:
    await members_by_both(session, submission.guardian_id, submission.dependent_id)
    try:
        body_from_draft(submission.draft)
    except ValidationError as exc:
        return agent.PolicyDraftValidation(
            valid=False, errors=[str(err["msg"]) for err in exc.errors()]
        )
    except ValueError as exc:
        return agent.PolicyDraftValidation(valid=False, errors=[str(exc)])
    return agent.PolicyDraftValidation(valid=True, errors=[])


async def settle_obligation(
    session: AsyncSession,
    obligation_id: str,
    body: "SettleObligation",
    *,
    camera_only: bool,
    fingerprint_key: bytes,
) -> "SettledObligation":
    """Settle a receipt owed for money already sent (SPEC §8.9).

    The same proof rules as the engine: the proof must belong to the obligation's request,
    count as proof (camera-only mode included), not have backed a transfer for any of the
    guardian's dependents, not match a receipt used in any family, and its findings must
    cover the amount. Its keys are spent against the transfer it settles, under the same
    reservoir lock the engine takes, so one receipt still backs one payment.
    """
    obligation_uuid = parse_uuid(obligation_id)
    found = await session.scalar(
        select(ProofObligation).where(ProofObligation.id == obligation_uuid)
    )
    if found is None:
        raise NotFoundError("No such proof obligation.")
    family = await session.get(Family, found.family_id)
    assert family is not None

    await lock_reservoir(session, family.guardian_token_account)
    obligation = await session.scalar(
        select(ProofObligation)
        .where(ProofObligation.id == obligation_uuid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert obligation is not None
    if obligation.status is ObligationStatus.SETTLED:
        return SettledObligation(settled=True, reason=None)

    proof_row = await session.scalar(
        select(Proof).where(
            Proof.id == parse_uuid(body.proof_id),
            Proof.request_id == obligation.request_id,
            Proof.family_id == family.id,
        )
    )
    if proof_row is None:
        return SettledObligation(settled=False, reason="proof not for this request")

    findings = body.findings
    if not findings.matches_claim or findings.total < obligation.amount:
        return SettledObligation(settled=False, reason="proof does not cover the amount")

    dependent_id = str(family.dependent_user_id)
    proof = to_agent_proof(proof_row, dependent_id, with_content=False)
    origin = None
    origin_uuid = parse_uuid(proof.derived_from)
    if origin_uuid is not None:
        origin_row = await session.scalar(
            select(Proof).where(Proof.id == origin_uuid, Proof.family_id == family.id)
        )
        if origin_row is not None:
            origin = to_agent_proof(origin_row, dependent_id, with_content=False)
    if not counts_as_proof(proof, camera_only=camera_only, origin=origin):
        # An unsigned PDF lands here too: it is stored, but it settles nothing.
        return SettledObligation(settled=False, reason="not usable as proof")

    keys = proof_keys(proof, findings)
    used = await session.scalars(
        select(UsedProofKey.key).where(UsedProofKey.guardian_user_id == family.guardian_user_id)
    )
    fingerprint = keyed_fingerprint(findings, fingerprint_key)
    receipt_used = await session.scalar(
        select(ReceiptFingerprint.key_hash).where(ReceiptFingerprint.key_hash == fingerprint)
    )
    if set(keys) & set(used) or receipt_used is not None:
        return SettledObligation(settled=False, reason="proof already used")

    obligation.status = ObligationStatus.SETTLED
    obligation.proof_id = proof_row.id
    obligation.findings = findings.model_dump()
    obligation.settled_at = datetime.now(UTC)
    for key in keys:
        session.add(
            UsedProofKey(
                guardian_user_id=family.guardian_user_id,
                key=key,
                family_id=family.id,
                transfer_id=obligation.transfer_id,
            )
        )
    session.add(
        ReceiptFingerprint(
            key_hash=fingerprint, family_id=family.id, transfer_id=obligation.transfer_id
        )
    )
    await audit_log.append(
        session,
        family.id,
        AuditEvent.PROOF_SETTLED,
        {"obligation_id": str(obligation.id), "proof_id": str(proof_row.id)},
    )
    try:
        await session.commit()
    except IntegrityError:
        # The receipt was spent by a transfer in another family between check and commit.
        await session.rollback()
        return SettledObligation(settled=False, reason="proof already used")
    return SettledObligation(settled=True, reason=None)
