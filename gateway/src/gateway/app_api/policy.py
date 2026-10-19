"""Guardian policy (SPEC §6, §14.1): drafts come from the policy interview
(`interview.py`); the guardian signs one to activate it.

Nothing the agent returns becomes a policy until the guardian's wallet signs the activation
message over the draft's version and hash (SPEC SEC-04).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app_api import nonces
from gateway.app_api.sessions import FamilyGuardianDep, MemberDep
from gateway.contracts.agent import PolicyDraft, TestScenario
from gateway.contracts.app import NonceValue, SignatureB58
from gateway.contracts.policy import PolicyBody, body_from_draft, policy_hash
from gateway.contracts.signing import activate_policy_message
from gateway.core.crypto import verify_ed25519
from gateway.core.errors import ConflictError, NotFoundError, UnauthorizedError
from gateway.db import audit_log
from gateway.db.models import Policy, PolicyProposal
from gateway.db.session import SessionDep
from gateway.engine.transfers import parse_uuid
from gateway.enums import AuditEvent, NoncePurpose, PolicyStatus, ProposalStatus, Role

router = APIRouter(prefix="/api/v1/policy", tags=["policy"])


class DraftView(BaseModel):
    """A stored draft: the exact body (base units) and hash the guardian signs, by version."""

    id: str
    version: int
    body: PolicyBody
    body_hash: str
    test_scenarios: list[TestScenario]
    open_questions: list[str]


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: NonceValue
    signature_b58: SignatureB58


class ActivePolicy(BaseModel):
    id: str
    version: int
    body: dict[str, Any]  # dependents get the limits without the guardian's guidance
    body_hash: str | None
    activated_at: datetime | None


class ProposalView(BaseModel):
    id: str
    change: str
    evidence: str
    created_at: datetime


def draft_view(row: Policy) -> DraftView:
    return DraftView(
        id=str(row.id),
        version=row.version,
        body=PolicyBody.model_validate(row.body),
        body_hash=row.body_hash,
        test_scenarios=[TestScenario.model_validate(s) for s in row.test_scenarios],
        open_questions=list(row.open_questions),
    )


async def store_draft(session: AsyncSession, family_id: uuid.UUID, draft: PolicyDraft) -> Policy:
    """The agent's draft (display units) as the next numbered draft in base units: the exact
    body and hash the guardian will sign. Raises `ValueError` if it is not a valid policy.
    The caller commits."""
    policy_body = body_from_draft(draft)
    latest = await session.scalar(
        select(func.max(Policy.version)).where(Policy.family_id == family_id)
    )
    row = Policy(
        family_id=family_id,
        version=(latest or 0) + 1,
        body=policy_body.model_dump(mode="json"),
        body_hash=policy_hash(policy_body),
        test_scenarios=[scenario.model_dump() for scenario in draft.test_scenarios],
        open_questions=list(draft.open_questions),
        status=PolicyStatus.DRAFT,
    )
    session.add(row)
    await session.flush()
    return row


async def _family_draft(session: SessionDep, family_id: uuid.UUID, draft_id: str) -> Policy:
    draft_uuid = parse_uuid(draft_id)
    row = None
    if draft_uuid is not None:
        row = await session.scalar(
            select(Policy).where(Policy.id == draft_uuid, Policy.family_id == family_id)
        )
    if row is None or row.status != PolicyStatus.DRAFT:
        raise NotFoundError("No such draft.")
    return row


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: str, guardian: FamilyGuardianDep, session: SessionDep) -> DraftView:
    return draft_view(await _family_draft(session, guardian.family.id, draft_id))


@router.post("/drafts/{draft_id}/activate")
async def activate(
    draft_id: str, body: ActivateRequest, guardian: FamilyGuardianDep, session: SessionDep
) -> ActivePolicy:
    family = guardian.family
    now = datetime.now(UTC)
    draft = await _family_draft(session, family.id, draft_id)
    wallet = guardian.user.wallet_address
    await nonces.consume(session, wallet, NoncePurpose.ACTIVATE_POLICY, body.nonce, now)
    # Recompute the hash from the stored body: the guardian signs what will be enforced.
    if policy_hash(PolicyBody.model_validate(draft.body)) != draft.body_hash:
        raise ConflictError("Draft is corrupted; create a new one.")
    message = activate_policy_message(family.id, draft.version, draft.body_hash, body.nonce)
    if not verify_ed25519(wallet, message, body.signature_b58):
        raise UnauthorizedError("Activation signature does not verify.")

    await session.execute(
        update(Policy)
        .where(Policy.family_id == family.id, Policy.status == PolicyStatus.ACTIVE)
        .values(status=PolicyStatus.SUPERSEDED)
    )
    draft.status = PolicyStatus.ACTIVE
    draft.guardian_signature = body.signature_b58
    draft.activation_nonce = body.nonce
    draft.activated_at = now
    await session.flush()
    await audit_log.append(
        session,
        family.id,
        AuditEvent.POLICY_ACTIVATED,
        {"policy_id": str(draft.id), "version": draft.version, "body_hash": draft.body_hash},
    )
    await session.commit()
    return ActivePolicy(
        id=str(draft.id),
        version=draft.version,
        body=draft.body,
        body_hash=draft.body_hash,
        activated_at=draft.activated_at,
    )


@router.get("/active")
async def get_active(member: MemberDep, session: SessionDep) -> ActivePolicy:
    row = await session.scalar(
        select(Policy).where(
            Policy.family_id == member.family.id, Policy.status == PolicyStatus.ACTIVE
        )
    )
    if row is None:
        raise NotFoundError("No active policy.")
    if member.role == Role.DEPENDENT:
        # Outer limits only: the guardian's guidance stays private (SPEC SEC-10, §15).
        limits = {k: v for k, v in row.body.items() if k != "guidance"}
        return ActivePolicy(
            id=str(row.id), version=row.version, body=limits, body_hash=None, activated_at=None
        )
    return ActivePolicy(
        id=str(row.id),
        version=row.version,
        body=row.body,
        body_hash=row.body_hash,
        activated_at=row.activated_at,
    )


@router.get("/proposals")
async def list_proposals(guardian: FamilyGuardianDep, session: SessionDep) -> list[ProposalView]:
    """The agent's "stop asking" suggestions. Adopting one means drafting and signing a new
    policy version; nothing here changes the active policy."""
    rows = await session.scalars(
        select(PolicyProposal)
        .where(
            PolicyProposal.family_id == guardian.family.id,
            PolicyProposal.status == ProposalStatus.OPEN,
        )
        .order_by(PolicyProposal.created_at)
    )
    return [
        ProposalView(id=str(p.id), change=p.change, evidence=p.evidence, created_at=p.created_at)
        for p in rows
    ]
