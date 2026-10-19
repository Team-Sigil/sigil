"""Database rows as the agent layer's contract objects.

Shared by the engine and the platform API, so the history the engine judges a transfer by is
the same history the agent sees in its context.
"""

import base64
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.contracts.agent import Category, HistoryItem, Outcome, Proof
from gateway.db.models import Decision, Transfer
from gateway.db.models import Proof as ProofRow
from gateway.enums import TransferStatus

HISTORY_LIMIT = 50

# Non-money outcomes that belong in history (the agent layer's "gap 2").
HISTORY_OUTCOMES = {
    Outcome.DECLINED,
    Outcome.ASKED_GUARDIAN,
    Outcome.AUDITOR_FLAGGED,
    Outcome.OUT_OF_SCOPE,
}


def as_category(value: str) -> Category:
    return Category(value) if value in Category else Category.OTHER


async def load_history(
    session: AsyncSession, family_id: uuid.UUID, limit: int = HISTORY_LIMIT
) -> list[HistoryItem]:
    """The family's most recent money events and non-money decisions, oldest first."""
    transfers = await session.scalars(
        select(Transfer)
        .where(Transfer.family_id == family_id, Transfer.status != TransferStatus.PENDING)
        .order_by(Transfer.created_at.desc())
        .limit(limit)
    )
    decisions = await session.scalars(
        select(Decision)
        .where(
            Decision.family_id == family_id,
            Decision.outcome.in_([o.value for o in HISTORY_OUTCOMES]),
        )
        .order_by(Decision.created_at.desc())
        .limit(limit)
    )
    items = [
        HistoryItem(
            at=t.created_at,
            kind="transfer" if t.status == TransferStatus.SENT else "blocked",
            amount=t.amount,
            category=as_category(t.category),
            purpose=t.purpose,
        )
        for t in transfers
    ] + [
        HistoryItem(at=d.created_at, kind=d.outcome, amount=0, category=Category.OTHER)
        for d in decisions
    ]
    items.sort(key=lambda item: item.at)
    return items[-limit:]


def to_agent_proof(row: ProofRow, dependent_id: str, with_content: bool) -> Proof:
    """A stored proof with its integrity metadata. Content is included only for GET /proofs.

    An unsigned PDF is the one case where the content is withheld: nothing ties it to the
    shop it claims to come from, so the model is told it exists and never shown it
    (SPEC §8.9).
    """
    is_text = row.media_type.startswith("text/")
    unsigned_document = row.media_type == "application/pdf" and "issuer_signed" not in row.flags
    data_b64 = None
    text = None
    if with_content and not unsigned_document:
        if is_text:
            text = row.content.decode()
        else:
            data_b64 = base64.b64encode(row.content).decode()
    return Proof(
        id=str(row.id),
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        data_b64=data_b64,
        text=text,
        request_id=str(row.request_id),
        dependent_id=dependent_id,
        uploaded_at=row.uploaded_at,
        sha256=row.sha256,
        source=row.source,
        derived_from=str(row.derived_from) if row.derived_from else None,
        flags=list(row.flags),
    )
