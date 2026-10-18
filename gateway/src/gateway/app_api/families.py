"""Who am I, and family registration (SPEC §16.1 step 2)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.app_api import nonces
from gateway.app_api.sessions import GuardianDep, MemberDep, PrincipalDep, create_token
from gateway.contracts.app import NonceValue, Pubkey, SignatureB58, Timezone
from gateway.contracts.signing import register_dependent_message
from gateway.core.config import Settings
from gateway.core.crypto import verify_ed25519
from gateway.core.errors import ConflictError, UnauthorizedError
from gateway.db import audit_log
from gateway.db.models import Family, User
from gateway.db.session import SessionDep
from gateway.enums import AuditEvent, NoncePurpose, Role

router = APIRouter(prefix="/api/v1", tags=["families"])

Locale = str  # BCP 47 tag, e.g. "en", "zh-TW"


class Me(BaseModel):
    user_id: str
    wallet: str
    role: Role
    family_id: str | None
    display_name: str | None
    locale: str | None


class FamilyView(BaseModel):
    id: str
    guardian_id: str
    guardian_wallet: str
    dependent_id: str
    dependent_wallet: str
    dependent_name: str | None
    dependent_token_account: str
    guardian_token_account: str
    timezone: str
    frozen: bool
    created_at: datetime


class RegisterFamily(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependent_wallet: Pubkey
    dependent_token_account: Pubkey
    # The guardian's aUSD token account (the reservoir). The engine only spends from it while
    # the crypto layer reports the guardian as its owner.
    guardian_token_account: Pubkey
    timezone: Timezone = "Asia/Bangkok"
    dependent_name: str | None = Field(default=None, max_length=80)
    guardian_locale: Locale | None = Field(default=None, max_length=35)
    dependent_locale: Locale | None = Field(default=None, max_length=35)
    nonce: NonceValue
    registration_signature_b58: SignatureB58


class RegisteredFamily(BaseModel):
    family: FamilyView
    token: str  # a new session that carries the family


async def family_view(session: AsyncSession, family: Family) -> FamilyView:
    guardian = await session.get(User, family.guardian_user_id)
    dependent = await session.get(User, family.dependent_user_id)
    assert guardian is not None and dependent is not None
    return FamilyView(
        id=str(family.id),
        guardian_id=str(guardian.id),
        guardian_wallet=guardian.wallet_address,
        dependent_id=str(dependent.id),
        dependent_wallet=dependent.wallet_address,
        dependent_name=dependent.display_name,
        dependent_token_account=family.dependent_token_account,
        guardian_token_account=family.guardian_token_account,
        timezone=family.timezone,
        frozen=family.frozen,
        created_at=family.created_at,
    )


@router.get("/me")
async def me(principal: PrincipalDep) -> Me:
    return Me(
        user_id=str(principal.user.id),
        wallet=principal.user.wallet_address,
        role=principal.role,
        family_id=str(principal.family.id) if principal.family else None,
        display_name=principal.user.display_name,
        locale=principal.user.locale,
    )


@router.get("/family")
async def get_family(member: MemberDep, session: SessionDep) -> FamilyView:
    return await family_view(session, member.family)


@router.post("/families")
async def register_family(
    body: RegisterFamily, guardian: GuardianDep, request: Request, session: SessionDep
) -> RegisteredFamily:
    settings: Settings = request.app.state.settings
    now = datetime.now(UTC)
    if guardian.family is not None:
        raise ConflictError("You already have a family.")
    if body.dependent_wallet == guardian.user.wallet_address:
        raise ConflictError("The dependent must use a different wallet.")

    dependent = await session.scalar(
        select(User).where(User.wallet_address == body.dependent_wallet)
    )
    if dependent is not None:
        in_family = await session.scalar(
            select(Family.id).where(
                or_(
                    Family.guardian_user_id == dependent.id,
                    Family.dependent_user_id == dependent.id,
                )
            )
        )
        if in_family is not None:
            raise ConflictError("That wallet already belongs to a family.")

    await nonces.consume(
        session, guardian.user.wallet_address, NoncePurpose.REGISTER_DEPENDENT, body.nonce, now
    )
    message = register_dependent_message(
        None, body.dependent_wallet, body.dependent_token_account, body.nonce
    )
    if not verify_ed25519(guardian.user.wallet_address, message, body.registration_signature_b58):
        raise UnauthorizedError("Registration signature does not verify.")

    if dependent is None:
        dependent = User(wallet_address=body.dependent_wallet, role=Role.DEPENDENT)
        session.add(dependent)
    dependent.role = Role.DEPENDENT
    dependent.display_name = body.dependent_name or dependent.display_name
    dependent.locale = body.dependent_locale or dependent.locale
    guardian.user.locale = body.guardian_locale or guardian.user.locale
    session.add(guardian.user)
    await session.flush()

    family = Family(
        guardian_user_id=guardian.user.id,
        dependent_user_id=dependent.id,
        dependent_token_account=body.dependent_token_account,
        guardian_token_account=body.guardian_token_account,
        registration_signature=body.registration_signature_b58,
        registration_nonce=body.nonce,
        timezone=body.timezone,
    )
    session.add(family)
    await session.flush()
    await audit_log.append(
        session,
        family.id,
        AuditEvent.FAMILY_REGISTERED,
        {"guardian_id": str(guardian.user.id), "dependent_id": str(dependent.id)},
    )
    await session.commit()
    await session.refresh(family)
    return RegisteredFamily(
        family=await family_view(session, family),
        token=create_token(settings, guardian.user, Role.GUARDIAN, family, now),
    )
