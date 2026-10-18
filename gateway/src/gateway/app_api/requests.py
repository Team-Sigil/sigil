"""Dependent money requests and their conversations (SPEC §16.2).

Every new message or proof is stored, then the agent runs on the request
(`POST /v1/requests/{id}/process`). If the agent is unavailable the message is kept and the
app can retry with `POST /requests/{id}/process`.
"""

import hashlib
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.agent_client import AgentDecision, AgentService
from gateway.app_api.sessions import FamilyDependentDep, Member, MemberDep
from gateway.capture.config import CaptureConfig
from gateway.capture.service import verify_capture
from gateway.contracts.app import Units
from gateway.core.config import Settings
from gateway.core.errors import ConflictError, DomainError, NotFoundError
from gateway.db import audit_log
from gateway.db.models import Decision, Message, Proof
from gateway.db.models import Request as RequestRow
from gateway.db.session import SessionDep
from gateway.engine.transfers import parse_uuid
from gateway.enums import AuditEvent, Role, ThreadRole

router = APIRouter(prefix="/api/v1/requests", tags=["requests"])

MAX_MESSAGE_CHARS = 2_000
MAX_PROOF_BYTES = 5 * 1024 * 1024
MAX_PROOFS_PER_REQUEST = 3  # the agent reads the first 3; more would be silently ignored
LIST_LIMIT = 50


class PayloadTooLargeError(DomainError):
    code = "PAYLOAD_TOO_LARGE"
    status = 413
    message = "Proofs may be at most 5 MB."


class UnsupportedProofError(DomainError):
    code = "UNSUPPORTED_PROOF"
    status = 422
    message = "Proofs must be a JPEG, PNG, WebP or GIF image, or plain text."


class DailyLimitError(DomainError):
    code = "DAILY_LIMIT_REACHED"
    status = 429
    message = "You've reached today's message limit. Try again tomorrow."


async def enforce_daily_limit(session: AsyncSession, member: Member, limit: int) -> None:
    """Every dependent message runs the agent, so each dependent gets a daily allowance of them
    (agent layer handoff item 8). The day is the family's calendar day."""
    zone = ZoneInfo(member.family.timezone)
    local_midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = await session.scalar(
        select(func.count(Message.id))
        .join(RequestRow, RequestRow.id == Message.request_id)
        .where(
            RequestRow.family_id == member.family.id,
            Message.role == ThreadRole.DEPENDENT,
            Message.created_at >= local_midnight,
        )
    )
    if (sent_today or 0) >= limit:
        raise DailyLimitError()


def _daily_limit(request: Request) -> int:
    settings: Settings = request.app.state.settings
    return settings.dependent_messages_per_day


class NewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    # Base units. The agent never sends more than this and never guesses an amount from text.
    amount_requested: Units | None = None
    emergency_claimed: bool = False


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    amount_requested: Units | None = None


class DecisionView(BaseModel):
    outcome: str
    reply_to_dependent: str
    amount_sent: int
    transfer_id: str | None = None  # the transfer this run produced, if any
    summary_for_guardian: str | None = None  # guardians only
    created_at: datetime | None = None


class RequestView(BaseModel):
    id: str
    message: str
    amount_requested: int | None
    emergency_claimed: bool
    already_sent: int
    created_at: datetime


class ThreadEntry(BaseModel):
    role: ThreadRole
    text: str
    at: datetime


class ProofView(BaseModel):
    id: str
    kind: str
    media_type: str
    size_bytes: int
    source: str  # "photo" only for a verified in-app capture
    flags: list[str]
    captured_at: datetime | None
    uploaded_at: datetime


class RequestDetail(RequestView):
    thread: list[ThreadEntry]
    proofs: list[ProofView]
    decisions: list[DecisionView]


class ProcessedRequest(BaseModel):
    request: RequestView
    decision: DecisionView


class UploadedProof(BaseModel):
    proof: ProofView
    decision: DecisionView


def _request_view(row: RequestRow) -> RequestView:
    return RequestView(
        id=str(row.id),
        message=row.message,
        amount_requested=row.amount_requested,
        emergency_claimed=row.emergency_claimed,
        already_sent=row.already_sent,
        created_at=row.created_at,
    )


def _proof_view(row: Proof) -> ProofView:
    return ProofView(
        id=str(row.id),
        kind=row.kind,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        source=row.source,
        flags=list(row.flags),
        captured_at=row.captured_at,
        uploaded_at=row.uploaded_at,
    )


def _decision_view(decision: AgentDecision) -> DecisionView:
    return DecisionView(
        outcome=decision.outcome,
        reply_to_dependent=decision.reply_to_dependent,
        amount_sent=decision.amount_sent,
    )


async def family_request(session: AsyncSession, member: Member, request_id: str) -> RequestRow:
    request_uuid = parse_uuid(request_id)
    row = None
    if request_uuid is not None:
        row = await session.scalar(
            select(RequestRow).where(
                RequestRow.id == request_uuid, RequestRow.family_id == member.family.id
            )
        )
    if row is None:
        raise NotFoundError("No such request.")
    return row


async def run_agent(request: Request, request_id: str) -> DecisionView:
    agent: AgentService = request.app.state.agent
    return _decision_view(await agent.process_request(request_id))


def sniff_image(data: bytes) -> str | None:
    """The image type from its bytes, never from the client's Content-Type."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.post("")
async def create_request(
    body: NewRequest, dependent: FamilyDependentDep, request: Request, session: SessionDep
) -> ProcessedRequest:
    await enforce_daily_limit(session, dependent, _daily_limit(request))
    row = RequestRow(
        family_id=dependent.family.id,
        message=body.message,
        amount_requested=body.amount_requested,
        emergency_claimed=body.emergency_claimed,
    )
    session.add(row)
    await session.flush()
    session.add(Message(request_id=row.id, role=ThreadRole.DEPENDENT, text=body.message))
    await audit_log.append(
        session, dependent.family.id, AuditEvent.REQUEST_CREATED, {"request_id": str(row.id)}
    )
    await session.commit()
    decision = await run_agent(request, str(row.id))
    await session.refresh(row)
    return ProcessedRequest(request=_request_view(row), decision=decision)


@router.post("/{request_id}/messages")
async def add_message(
    request_id: str,
    body: NewMessage,
    dependent: FamilyDependentDep,
    request: Request,
    session: SessionDep,
) -> ProcessedRequest:
    row = await family_request(session, dependent, request_id)
    await enforce_daily_limit(session, dependent, _daily_limit(request))
    if body.amount_requested is not None:
        if body.amount_requested < row.already_sent:
            raise ConflictError("The amount cannot be below what was already sent.")
        row.amount_requested = body.amount_requested
    session.add(Message(request_id=row.id, role=ThreadRole.DEPENDENT, text=body.message))
    await session.commit()
    decision = await run_agent(request, str(row.id))
    await session.refresh(row)
    return ProcessedRequest(request=_request_view(row), decision=decision)


@router.post("/{request_id}/process")
async def process(
    request_id: str, member: MemberDep, request: Request, session: SessionDep
) -> ProcessedRequest:
    """Runs the agent again, e.g. after a timeout. Safe for money: transfers are idempotent."""
    row = await family_request(session, member, request_id)
    decision = await run_agent(request, str(row.id))
    await session.refresh(row)
    return ProcessedRequest(request=_request_view(row), decision=decision)


@router.post("/{request_id}/proofs")
async def upload_proof(
    request_id: str,
    dependent: FamilyDependentDep,
    request: Request,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form(pattern="^(receipt|quote|photo)$")] = "receipt",
    capture_claim: Annotated[str | None, Form(max_length=2_000)] = None,
    capture_signature: Annotated[str | None, Form(max_length=4_000)] = None,
) -> UploadedProof:
    row = await family_request(session, dependent, request_id)
    await enforce_daily_limit(session, dependent, _daily_limit(request))
    count = await session.scalar(select(func.count()).where(Proof.request_id == row.id))
    if (count or 0) >= MAX_PROOFS_PER_REQUEST:
        raise ConflictError("A request can have at most 3 proofs.")

    data = await file.read(MAX_PROOF_BYTES + 1)
    if len(data) > MAX_PROOF_BYTES:
        raise PayloadTooLargeError()
    media_type = sniff_image(data)
    # A plain upload is a "file"; typed text is only a claim; "photo" needs a verified capture.
    source = "file"
    verified = None
    if media_type is not None and capture_claim and capture_signature:
        config: CaptureConfig = request.app.state.capture
        verified = await verify_capture(
            session,
            config,
            dependent.user,
            row,
            data,
            capture_claim,
            capture_signature,
            datetime.now(UTC),
        )
        if verified is not None:
            source = "photo"
    if media_type is None:
        if not (file.content_type or "").startswith("text/plain") or not data:
            raise UnsupportedProofError()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedProofError() from exc
        media_type, source = "text/plain", "typed"

    digest = hashlib.sha256(data).hexdigest()
    seen = await session.scalar(
        select(Proof.id).where(Proof.family_id == dependent.family.id, Proof.sha256 == digest)
    )
    proof = Proof(
        request_id=row.id,
        family_id=dependent.family.id,
        kind=kind,
        media_type=media_type,
        size_bytes=len(data),
        content=data,
        sha256=digest,
        source=source,
        flags=(["duplicate"] if seen else []) + (["camera_signed"] if verified else []),
        device_id=verified.device_id if verified else None,
        captured_at=verified.captured_at if verified else None,
    )
    session.add(proof)
    await session.flush()
    session.add(Message(request_id=row.id, role=ThreadRole.DEPENDENT, text="(uploaded a proof)"))
    await audit_log.append(
        session,
        dependent.family.id,
        AuditEvent.PROOF_UPLOADED,
        {"request_id": str(row.id), "proof_id": str(proof.id), "sha256": digest, "source": source},
    )
    await session.commit()
    return UploadedProof(proof=_proof_view(proof), decision=await run_agent(request, str(row.id)))


@router.get("")
async def list_requests(member: MemberDep, session: SessionDep) -> list[RequestView]:
    rows = await session.scalars(
        select(RequestRow)
        .where(RequestRow.family_id == member.family.id)
        .order_by(RequestRow.created_at.desc())
        .limit(LIST_LIMIT)
    )
    return [_request_view(r) for r in rows]


@router.get("/{request_id}")
async def get_request(request_id: str, member: MemberDep, session: SessionDep) -> RequestDetail:
    row = await family_request(session, member, request_id)
    messages = await session.scalars(
        select(Message).where(Message.request_id == row.id).order_by(Message.created_at)
    )
    proofs = await session.scalars(
        select(Proof).where(Proof.request_id == row.id).order_by(Proof.uploaded_at)
    )
    decisions = await session.scalars(
        select(Decision).where(Decision.request_id == row.id).order_by(Decision.created_at)
    )
    is_guardian = member.role == Role.GUARDIAN
    return RequestDetail(
        **_request_view(row).model_dump(),
        thread=[ThreadEntry(role=m.role, text=m.text, at=m.created_at) for m in messages],
        proofs=[_proof_view(p) for p in proofs],
        decisions=[
            DecisionView(
                outcome=d.outcome,
                reply_to_dependent=d.reply_to_dependent,
                amount_sent=d.amount_sent,
                transfer_id=str(d.transfer_id) if d.transfer_id else None,
                summary_for_guardian=d.summary_for_guardian if is_guardian else None,
                created_at=d.created_at,
            )
            for d in decisions
        ],
    )
