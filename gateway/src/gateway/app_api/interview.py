"""The policy interview (agent layer policy-protocol.md, `sigil.policy-interview/1`).

The agent asks the guardian questions until the policy is complete, shows how it would
behave, and hands back a draft. The agent keeps no state: the gateway stores the conversation
and sends all of it on every turn.

Two rules keep "ready" honest. Agent turns are stored exactly as the agent returned them,
with their `kind`; and the app can only ever add the guardian's own message, never an agent
turn, so a client cannot fake the `review` turn that "ready" depends on. A ready draft still
does nothing until the guardian signs it (`POST /policy/drafts/{id}/activate`).
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.agent_client import (
    MAX_INTERVIEW_TURNS,
    AgentService,
    ChatTurn,
    ChecklistItem,
    InterviewProgress,
)
from gateway.app_api.policy import DraftView, draft_view, store_draft
from gateway.app_api.sessions import FamilyGuardianDep
from gateway.contracts.agent import PolicyDraft, PolicyDraftValidation
from gateway.core.errors import ConflictError, DomainError, NotFoundError
from gateway.db.models import Family, PolicyInterview, PolicyInterviewTurn
from gateway.db.session import SessionDep
from gateway.enums import InterviewRole, InterviewStatus, InterviewTurnKind

router = APIRouter(prefix="/api/v1/policy/interview", tags=["policy"])


class MessageRequiredError(DomainError):
    code = "INTERVIEW_MESSAGE_REQUIRED"
    status = 422
    message = "Answer the agent's question to continue."


class InterviewTooLongError(DomainError):
    code = "INTERVIEW_TOO_LONG"
    status = 409
    message = "This conversation is too long to continue. Start over to set up the policy."


class GuardianMessage(BaseModel):
    """Only the guardian's own words. Agent turns never come from the app."""

    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, min_length=1, max_length=4_000)


class InterviewView(BaseModel):
    interview_id: str
    status: Literal["needs_input", "ready", "unavailable"]
    phase: str | None
    question: str | None
    question_kind: str | None
    checklist: list[ChecklistItem]
    progress: InterviewProgress | None
    agent_draft: PolicyDraft | None  # live preview in display units, once every limit is set
    validation: PolicyDraftValidation | None
    draft: DraftView | None  # when ready: the exact draft to sign
    turns_used: int
    max_turns: int = MAX_INTERVIEW_TURNS


class TurnView(BaseModel):
    role: InterviewRole
    text: str
    kind: InterviewTurnKind | None
    at: datetime


class Transcript(BaseModel):
    interview_id: str
    status: InterviewStatus
    turns: list[TurnView]
    draft_policy_id: str | None


async def _open_interview(session: AsyncSession, family: Family) -> PolicyInterview:
    interview = await session.scalar(
        select(PolicyInterview).where(
            PolicyInterview.family_id == family.id,
            PolicyInterview.status == InterviewStatus.OPEN,
        )
    )
    if interview is not None:
        return interview
    interview = PolicyInterview(family_id=family.id)
    session.add(interview)
    try:
        await session.commit()
    except IntegrityError:
        # Another request opened it at the same moment; use that one.
        await session.rollback()
        return await _open_interview(session, family)
    return interview


async def _turns(session: AsyncSession, interview: PolicyInterview) -> list[PolicyInterviewTurn]:
    rows = await session.scalars(
        select(PolicyInterviewTurn)
        .where(PolicyInterviewTurn.interview_id == interview.id)
        .order_by(PolicyInterviewTurn.position)
    )
    return list(rows)


@router.post("")
async def take_turn(
    body: GuardianMessage, guardian: FamilyGuardianDep, request: Request, session: SessionDep
) -> InterviewView:
    """Starts the interview (no message) or answers the agent's latest question."""
    agent: AgentService = request.app.state.agent
    family = guardian.family
    interview = await _open_interview(session, family)
    turns = await _turns(session, interview)
    if turns and body.message is None:
        raise MessageRequiredError()
    # Room for the guardian's message and the agent's next question.
    if len(turns) + (1 if body.message else 0) + 1 > MAX_INTERVIEW_TURNS:
        raise InterviewTooLongError()

    conversation = [
        ChatTurn(role=t.role.value, text=t.text, kind=t.kind.value if t.kind else None)
        for t in turns
    ]
    if body.message is not None:
        conversation.append(ChatTurn(role="guardian", text=body.message))
    interview_id = interview.id
    # The agent call can take minutes, so no transaction is held across it.
    await session.commit()

    result = await agent.interview_turn(
        str(family.guardian_user_id),
        str(family.dependent_user_id),
        conversation,
        guardian.user.locale,
    )
    view = InterviewView(
        interview_id=str(interview_id),
        status=result.status,
        phase=result.phase,
        question=result.question,
        question_kind=result.question_kind,
        checklist=result.checklist,
        progress=result.progress,
        agent_draft=result.draft,
        validation=result.validation,
        draft=None,
        turns_used=len(turns),
    )
    if result.status == "unavailable":
        return view  # nothing changed; retry later with the same message

    # Store the turn only if nobody else moved the interview on meanwhile.
    locked = await session.scalar(
        select(PolicyInterview).where(PolicyInterview.id == interview_id).with_for_update()
    )
    count = await session.scalar(
        select(func.count()).where(PolicyInterviewTurn.interview_id == interview_id)
    )
    if locked is None or locked.status != InterviewStatus.OPEN or count != len(turns):
        await session.rollback()
        raise ConflictError("The interview changed in the meantime; reload it.")

    position = len(turns)
    if body.message is not None:
        session.add(
            PolicyInterviewTurn(
                interview_id=interview_id,
                position=position,
                role=InterviewRole.GUARDIAN,
                text=body.message,
            )
        )
        position += 1

    if result.status == "needs_input":
        assert result.question is not None and result.question_kind is not None
        session.add(
            PolicyInterviewTurn(
                interview_id=interview_id,
                position=position,
                role=InterviewRole.AGENT,
                text=result.question,
                kind=InterviewTurnKind(result.question_kind),
            )
        )
        position += 1
    else:  # ready
        assert result.draft is not None
        try:
            row = await store_draft(session, family.id, result.draft)
        except ValueError as exc:
            # The agent's checks passed but ours did not; ours is what gets signed.
            await session.rollback()
            raise ConflictError(
                "The proposed policy does not fit together; tell the agent what to change."
            ) from exc
        locked.status = InterviewStatus.COMPLETED
        locked.draft_policy_id = row.id
        view.draft = draft_view(row)

    await session.commit()
    view.turns_used = position
    return view


@router.get("")
async def transcript(guardian: FamilyGuardianDep, session: SessionDep) -> Transcript:
    """The open interview, or the most recent one."""
    interview = await session.scalar(
        select(PolicyInterview)
        .where(PolicyInterview.family_id == guardian.family.id)
        .order_by(
            (PolicyInterview.status == InterviewStatus.OPEN).desc(),
            PolicyInterview.created_at.desc(),
        )
        .limit(1)
    )
    if interview is None:
        raise NotFoundError("No policy interview yet.")
    return Transcript(
        interview_id=str(interview.id),
        status=interview.status,
        turns=[
            TurnView(role=t.role, text=t.text, kind=t.kind, at=t.created_at)
            for t in await _turns(session, interview)
        ],
        draft_policy_id=str(interview.draft_policy_id) if interview.draft_policy_id else None,
    )


@router.delete("")
async def start_over(guardian: FamilyGuardianDep, session: SessionDep) -> Transcript:
    interview = await session.scalar(
        select(PolicyInterview)
        .where(
            PolicyInterview.family_id == guardian.family.id,
            PolicyInterview.status == InterviewStatus.OPEN,
        )
        .with_for_update()
    )
    if interview is None:
        raise NotFoundError("No open policy interview.")
    interview.status = InterviewStatus.ABANDONED
    await session.commit()
    return Transcript(
        interview_id=str(interview.id), status=interview.status, turns=[], draft_policy_id=None
    )


@router.get("/protocol")
async def protocol(
    guardian: FamilyGuardianDep, request: Request, locale: str | None = None
) -> dict[str, Any]:
    """The checklist and phases, with labels in the guardian's language, to render progress."""
    agent: AgentService = request.app.state.agent
    return await agent.interview_protocol(locale or guardian.user.locale)
