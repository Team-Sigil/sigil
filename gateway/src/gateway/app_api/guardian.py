"""The guardian's signed decisions: answers to the agent's questions and the freeze switch.

Both count only with a wallet signature over a single-use nonce (SPEC SEC-05, SEC-17); what
anyone says in chat never does.
"""

from datetime import UTC, datetime
from typing import Literal, Self

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from gateway.agent_client import AgentUnavailableError
from gateway.app_api import nonces
from gateway.app_api.requests import DecisionView, run_agent
from gateway.app_api.sessions import FamilyGuardianDep
from gateway.contracts.app import NonceValue, SignatureB58, Units
from gateway.contracts.signing import answer_question_message, freeze_message
from gateway.core.crypto import verify_ed25519
from gateway.core.errors import ConflictError, DomainError, NotFoundError, UnauthorizedError
from gateway.db import audit_log
from gateway.db.models import Family, GuardianQuestion, Message
from gateway.db.models import Request as RequestRow
from gateway.db.session import SessionDep
from gateway.engine.transfers import parse_uuid
from gateway.enums import AuditEvent, GuardianDecision, NoncePurpose, QuestionStatus, ThreadRole
from gateway.money import to_display

router = APIRouter(prefix="/api/v1", tags=["guardian"])


class InvalidAnswerError(DomainError):
    code = "INVALID_ANSWER"
    status = 422


class QuestionView(BaseModel):
    id: str
    request_id: str | None
    question: str
    recommendation: str
    recommended_amount: int | None
    urgency: str
    agent_view: str
    auditor_view: str | None
    status: QuestionStatus
    decision: GuardianDecision | None
    approved_amount: int | None
    note: str | None
    created_at: datetime
    answered_at: datetime | None


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: GuardianDecision
    amount_units: Units | None = None  # required to approve; must be absent to deny
    note: str | None = Field(default=None, max_length=300)
    nonce: NonceValue
    signature_b58: SignatureB58

    @model_validator(mode="after")
    def amount_matches_decision(self) -> Self:
        if self.decision == GuardianDecision.APPROVE and self.amount_units is None:
            raise ValueError("an approval needs amount_units")
        if self.decision == GuardianDecision.DENY and self.amount_units is not None:
            raise ValueError("a denial has no amount_units")
        return self


class Answered(BaseModel):
    question: QuestionView
    decision: DecisionView | None  # the agent's next move, when it could run


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frozen: bool
    nonce: NonceValue
    signature_b58: SignatureB58


class FreezeState(BaseModel):
    frozen: bool


def _question_view(q: GuardianQuestion) -> QuestionView:
    return QuestionView(
        id=str(q.id),
        request_id=str(q.request_id) if q.request_id else None,
        question=q.question,
        recommendation=q.recommendation,
        recommended_amount=q.recommended_amount,
        urgency=q.urgency,
        agent_view=q.agent_view,
        auditor_view=q.auditor_view,
        status=q.status,
        decision=q.decision,
        approved_amount=q.approved_amount,
        note=q.note,
        created_at=q.created_at,
        answered_at=q.answered_at,
    )


@router.get("/questions")
async def list_questions(
    guardian: FamilyGuardianDep,
    session: SessionDep,
    status: Literal["open", "answered"] = "open",
) -> list[QuestionView]:
    rows = await session.scalars(
        select(GuardianQuestion)
        .where(
            GuardianQuestion.family_id == guardian.family.id,
            GuardianQuestion.status == QuestionStatus(status),
        )
        .order_by(GuardianQuestion.created_at)
    )
    return [_question_view(q) for q in rows]


@router.post("/questions/{question_id}/answer")
async def answer_question(
    question_id: str,
    body: Answer,
    guardian: FamilyGuardianDep,
    request: Request,
    session: SessionDep,
) -> Answered:
    family = guardian.family
    now = datetime.now(UTC)
    question_uuid = parse_uuid(question_id)
    question = None
    if question_uuid is not None:
        question = await session.scalar(
            select(GuardianQuestion)
            .where(GuardianQuestion.id == question_uuid, GuardianQuestion.family_id == family.id)
            .with_for_update()
        )
    if question is None:
        raise NotFoundError("No such question.")
    if question.status != QuestionStatus.OPEN:
        raise ConflictError("This question was already answered.")

    target = None
    if question.request_id is not None:
        target = await session.get(RequestRow, question.request_id)
    if target is not None and body.amount_units is not None:
        still_open = (target.amount_requested or 0) - target.already_sent
        if body.amount_units > still_open:
            raise InvalidAnswerError("You can approve at most what is still requested.")

    wallet = guardian.user.wallet_address
    await nonces.consume(session, wallet, NoncePurpose.ANSWER_QUESTION, body.nonce, now)
    message = answer_question_message(
        family.id, question.id, body.decision, body.amount_units, body.nonce
    )
    if not verify_ed25519(wallet, message, body.signature_b58):
        raise UnauthorizedError("Answer signature does not verify.")

    question.status = QuestionStatus.ANSWERED
    question.decision = body.decision
    question.approved_amount = body.amount_units
    question.note = body.note
    question.signature = body.signature_b58
    question.answer_nonce = body.nonce
    question.answered_at = now
    if target is not None:
        # The agent sees the verified answer in the thread; the engine checks the signature
        # again itself before letting any money through.
        if body.decision == GuardianDecision.APPROVE and body.amount_units is not None:
            text = f"The guardian approved {to_display(body.amount_units)}."
        else:
            text = "The guardian declined."
        if body.note:
            text += f" Note: {body.note}"
        session.add(Message(request_id=target.id, role=ThreadRole.GUARDIAN_ANSWER, text=text))
    await audit_log.append(
        session,
        family.id,
        AuditEvent.QUESTION_ANSWERED,
        {
            "question_id": str(question.id),
            "decision": body.decision.value,
            "amount": body.amount_units,
        },
    )
    await session.commit()

    decision = None
    if target is not None:
        try:
            decision = await run_agent(request, str(target.id))
        except AgentUnavailableError:
            decision = None  # the answer is recorded; the app can retry /process later
    return Answered(question=_question_view(question), decision=decision)


@router.post("/freeze")
async def set_freeze(
    body: FreezeRequest, guardian: FamilyGuardianDep, session: SessionDep
) -> FreezeState:
    family = guardian.family
    wallet = guardian.user.wallet_address
    await nonces.consume(session, wallet, NoncePurpose.FREEZE, body.nonce, datetime.now(UTC))
    if not verify_ed25519(
        wallet, freeze_message(family.id, body.frozen, body.nonce), body.signature_b58
    ):
        raise UnauthorizedError("Freeze signature does not verify.")
    row = await session.get(Family, family.id, with_for_update=True)
    assert row is not None
    row.frozen = body.frozen
    await audit_log.append(session, family.id, AuditEvent.FREEZE_CHANGED, {"frozen": body.frozen})
    await session.commit()
    return FreezeState(frozen=body.frozen)
