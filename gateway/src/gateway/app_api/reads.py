"""What the app shows: budget, history, notifications, the audit trail and live events."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from gateway.app_api.sessions import FamilyGuardianDep, MemberDep, PrincipalDep
from gateway.contracts.agent import HistoryItem
from gateway.contracts.crypto import CryptoClient, CryptoUnavailableError
from gateway.contracts.policy import PolicyBody
from gateway.core.config import Settings
from gateway.core.errors import NotFoundError
from gateway.db import audit_log
from gateway.db.models import (
    BudgetCounter,
    Notification,
    Policy,
    ProofObligation,
    Transfer,
    User,
)
from gateway.db.session import SessionDep
from gateway.engine.transfers import (
    parse_uuid,
    reservoir_pending,
    usable_delegation,
    week_start,
)
from gateway.enums import ObligationStatus, PolicyStatus, TransferStatus
from gateway.events import EventBus
from gateway.views import load_history

router = APIRouter(prefix="/api/v1", tags=["reads"])

LIST_LIMIT = 50
HEARTBEAT_SECONDS = 15


class Budget(BaseModel):
    week_start: date
    spent: int
    reserved: int
    weekly_cap: int
    remaining: int
    delegated_remaining: int | None  # None when the crypto layer cannot be read


class TransferView(BaseModel):
    id: str
    request_id: str | None
    trigger: str
    amount: int
    category: str
    purpose: str
    status: TransferStatus
    reason: str | None
    tx_signature: str | None
    created_at: datetime


class History(BaseModel):
    items: list[HistoryItem]
    transfers: list[TransferView]


class ProofObligationView(BaseModel):
    """A receipt owed for money already sent. While one is open the dependent cannot ask
    for more (SPEC §8.9)."""

    id: str
    request_id: str
    transfer_id: str
    amount: int
    created_at: datetime


class NotificationView(BaseModel):
    id: str
    family_id: str | None  # None: about all of a guardian's dependents (e.g. the reservoir)
    message: str
    created_at: datetime
    read_at: datetime | None


class AuditEntry(BaseModel):
    id: int
    event_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime


class AuditTrail(BaseModel):
    entries: list[AuditEntry]
    chain_valid: bool  # recomputed end to end on every read (SPEC SEC-16)


@router.get("/budget")
async def get_budget(member: MemberDep, request: Request, session: SessionDep) -> Budget:
    settings: Settings = request.app.state.settings
    crypto: CryptoClient = request.app.state.crypto
    family = member.family
    week = week_start(datetime.now(UTC), family.timezone)
    counter = await session.scalar(
        select(BudgetCounter).where(
            BudgetCounter.family_id == family.id, BudgetCounter.week_start == week
        )
    )
    spent, reserved = (counter.spent, counter.reserved) if counter else (0, 0)
    policy = await session.scalar(
        select(Policy).where(Policy.family_id == family.id, Policy.status == PolicyStatus.ACTIVE)
    )
    cap = PolicyBody.model_validate(policy.body).weekly_cap if policy else 0

    guardian = await session.get(User, family.guardian_user_id)
    assert guardian is not None
    delegated: int | None
    try:
        delegation = await crypto.get_delegation(family.guardian_token_account)
        usable = usable_delegation(
            delegation, settings.agent_wallet_pubkey, guardian.wallet_address
        )
        # The reservoir is shared by all of the guardian's dependents.
        pending = await reservoir_pending(session, family.guardian_token_account)
        delegated = max(0, usable - pending)
    except CryptoUnavailableError:
        delegated = None
    return Budget(
        week_start=week,
        spent=spent,
        reserved=reserved,
        weekly_cap=cap,
        remaining=max(0, cap - spent - reserved),
        delegated_remaining=delegated,
    )


@router.get("/proof-obligations")
async def get_proof_obligations(
    member: MemberDep, session: SessionDep
) -> list[ProofObligationView]:
    rows = await session.scalars(
        select(ProofObligation)
        .where(
            ProofObligation.family_id == member.family.id,
            ProofObligation.status == ObligationStatus.OPEN,
        )
        .order_by(ProofObligation.created_at)
    )
    return [
        ProofObligationView(
            id=str(row.id),
            request_id=str(row.request_id),
            transfer_id=str(row.transfer_id),
            amount=row.amount,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/history")
async def get_history(member: MemberDep, session: SessionDep) -> History:
    transfers = await session.scalars(
        select(Transfer)
        .where(Transfer.family_id == member.family.id)
        .order_by(Transfer.created_at.desc())
        .limit(LIST_LIMIT)
    )
    return History(
        items=await load_history(session, member.family.id),
        transfers=[
            TransferView(
                id=str(t.id),
                request_id=str(t.request_id) if t.request_id else None,
                trigger=t.trigger.value,
                amount=t.amount,
                category=t.category,
                purpose=t.purpose,
                status=t.status,
                reason=t.reason,
                tx_signature=t.tx_signature,
                created_at=t.created_at,
            )
            for t in transfers
        ],
    )


@router.get("/notifications")
async def list_notifications(
    principal: PrincipalDep, session: SessionDep
) -> list[NotificationView]:
    """All of the caller's notifications, across a guardian's dependents."""
    rows = await session.scalars(
        select(Notification)
        .where(Notification.recipient_id == principal.user.id)
        .order_by(Notification.created_at.desc())
        .limit(LIST_LIMIT)
    )
    return [notification_view(n) for n in rows]


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: str, principal: PrincipalDep, session: SessionDep
) -> NotificationView:
    notification_uuid = parse_uuid(notification_id)
    row = None
    if notification_uuid is not None:
        row = await session.scalar(
            select(Notification).where(
                Notification.id == notification_uuid,
                Notification.recipient_id == principal.user.id,
            )
        )
    if row is None:
        raise NotFoundError("No such notification.")
    row.read_at = row.read_at or datetime.now(UTC)
    await session.commit()
    return notification_view(row)


def notification_view(n: Notification) -> NotificationView:
    return NotificationView(
        id=str(n.id),
        family_id=str(n.family_id) if n.family_id else None,
        message=n.message,
        created_at=n.created_at,
        read_at=n.read_at,
    )


@router.get("/audit")
async def get_audit(guardian: FamilyGuardianDep, session: SessionDep) -> AuditTrail:
    chain = await audit_log.load_chain(session, guardian.family.id)
    return AuditTrail(
        entries=[
            AuditEntry(
                id=e.id,
                event_type=e.event_type,
                payload=e.payload,
                prev_hash=e.prev_hash,
                hash=e.hash,
                created_at=e.created_at,
            )
            for e in chain
        ],
        chain_valid=audit_log.verify_entries(chain),
    )


@router.get("/events")
async def events(member: MemberDep, request: Request) -> StreamingResponse:
    """Server-Sent Events for the caller's family. Send the bearer token in the Authorization
    header (e.g. with fetch streaming); tokens never go in the URL."""
    bus: EventBus = request.app.state.events
    queue = bus.subscribe(member.family.id, member.role)

    async def stream() -> AsyncIterator[str]:
        try:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield event.encode()
        finally:
            bus.unsubscribe(member.family.id, member.role, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
