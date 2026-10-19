"""Append-only, hash-chained audit log (SPEC §12, SEC-16)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.crypto import canonical_json, sha256_hex
from gateway.db.models import GENESIS_HASH, AuditLog
from gateway.enums import AuditEvent


def format_timestamp(value: datetime) -> str:
    """ISO 8601 UTC with microseconds and `Z`, as hashed (SPEC §12)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_hash(
    prev_hash: str, event_type: str, payload: dict[str, Any], created_at: datetime
) -> str:
    entry = {
        "event_type": event_type,
        "payload": payload,
        "created_at": format_timestamp(created_at),
    }
    return sha256_hex(prev_hash + canonical_json(entry))


def chain_lock_key(family_id: uuid.UUID | None) -> str:
    return f"audit:{family_id if family_id is not None else 'system'}"


async def append(
    session: AsyncSession,
    family_id: uuid.UUID | None,
    event_type: AuditEvent,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> AuditLog:
    """Appends one entry inside the caller's transaction. The advisory lock serializes writers
    of the same chain until commit, so two appends can never share a `prev_hash`."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": chain_lock_key(family_id)}
    )
    family_filter = (
        AuditLog.family_id.is_(None) if family_id is None else AuditLog.family_id == family_id
    )
    prev_hash = await session.scalar(
        select(AuditLog.hash).where(family_filter).order_by(AuditLog.id.desc()).limit(1)
    )
    created_at = now or datetime.now(UTC)
    entry = AuditLog(
        family_id=family_id,
        event_type=event_type.value,
        payload=payload,
        prev_hash=prev_hash or GENESIS_HASH,
        hash=compute_hash(prev_hash or GENESIS_HASH, event_type.value, payload, created_at),
        created_at=created_at,
    )
    session.add(entry)
    await session.flush()
    return entry


def verify_entries(entries: Sequence[AuditLog]) -> bool:
    """True if `entries` (one chain, ordered by id) link up from genesis and every hash matches."""
    expected_prev = GENESIS_HASH
    for entry in entries:
        if entry.prev_hash != expected_prev:
            return False
        recomputed = compute_hash(
            entry.prev_hash, entry.event_type, entry.payload, entry.created_at
        )
        if entry.hash != recomputed:
            return False
        expected_prev = entry.hash
    return True


async def load_chain(session: AsyncSession, family_id: uuid.UUID | None) -> Sequence[AuditLog]:
    family_filter = (
        AuditLog.family_id.is_(None) if family_id is None else AuditLog.family_id == family_id
    )
    result = await session.scalars(select(AuditLog).where(family_filter).order_by(AuditLog.id))
    return result.all()


async def verify_chain(session: AsyncSession, family_id: uuid.UUID | None) -> bool:
    return verify_entries(await load_chain(session, family_id))
