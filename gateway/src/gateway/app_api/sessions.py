"""JWT sessions for the app layer and the role dependencies routes use.

The token only proves who the caller is (`sub` = user id). Role and family are read from the
database on every request, so a token never keeps a role or family the user no longer has.

A guardian may have several dependents, one family each. Family-scoped routes act on the
family named in the `X-Family-Id` header; a guardian with a single family may leave it out.
A dependent always acts on their own (only) family.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.config import Settings
from gateway.core.errors import DomainError, ForbiddenError, NotFoundError, UnauthorizedError
from gateway.db.models import Family, User
from gateway.db.session import SessionDep
from gateway.enums import Role

JWT_ALGORITHM = "HS256"


class FamilyRequiredError(DomainError):
    code = "FAMILY_REQUIRED"
    status = 400
    message = "You support several dependents: say which one with the X-Family-Id header."


@dataclass(frozen=True)
class Principal:
    user: User
    role: Role
    families: list[Family]  # a guardian's, oldest first; a dependent's one family
    family: Family | None  # the family this request acts on, if one is known


@dataclass(frozen=True)
class Member:
    """A caller who belongs to a family."""

    user: User
    role: Role
    family: Family


def create_token(
    settings: Settings, user: User, role: Role, family: Family | None, now: datetime
) -> str:
    claims = {
        "sub": str(user.id),
        "wallet": user.wallet_address,
        "role": role.value,
        "family_id": str(family.id) if family else None,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_ttl_hours)).timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


async def resolve(
    session: AsyncSession, user: User, requested_family: str | None = None
) -> Principal:
    """A wallet registered as a family's dependent is a dependent; every other wallet is a
    guardian, with any number of families (one per dependent)."""
    own = await session.scalar(select(Family).where(Family.dependent_user_id == user.id))
    if own is not None:
        if requested_family and requested_family != str(own.id):
            raise NotFoundError("No such family.")
        return Principal(user=user, role=Role.DEPENDENT, families=[own], family=own)

    families = list(
        await session.scalars(
            select(Family)
            .where(Family.guardian_user_id == user.id)
            .order_by(Family.created_at, Family.id)
        )
    )
    if requested_family:
        chosen = next((f for f in families if str(f.id) == requested_family), None)
        if chosen is None:
            # Another guardian's family looks exactly like one that doesn't exist.
            raise NotFoundError("No such family.")
    else:
        chosen = families[0] if len(families) == 1 else None
    return Principal(user=user, role=Role.GUARDIAN, families=families, family=chosen)


async def current_principal(
    request: Request,
    session: SessionDep,
    authorization: str = Header(default=""),
    x_family_id: str | None = Header(default=None, max_length=64),
) -> Principal:
    settings: Settings = request.app.state.settings
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Bearer token required.")
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise UnauthorizedError("Invalid or expired token.") from exc
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise UnauthorizedError("Unknown user.")
    return await resolve(session, user, x_family_id)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _member(principal: PrincipalDep) -> Member:
    if principal.family is None:
        if principal.families:
            raise FamilyRequiredError()
        raise ForbiddenError("Register a family first.")
    return Member(user=principal.user, role=principal.role, family=principal.family)


def _guardian(principal: PrincipalDep) -> Principal:
    if principal.role != Role.GUARDIAN:
        raise ForbiddenError("Guardians only.")
    return principal


def _family_guardian(member: Annotated[Member, Depends(_member)]) -> Member:
    if member.role != Role.GUARDIAN:
        raise ForbiddenError("Guardians only.")
    return member


def _family_dependent(member: Annotated[Member, Depends(_member)]) -> Member:
    if member.role != Role.DEPENDENT:
        raise ForbiddenError("Dependents only.")
    return member


MemberDep = Annotated[Member, Depends(_member)]
GuardianDep = Annotated[Principal, Depends(_guardian)]
FamilyGuardianDep = Annotated[Member, Depends(_family_guardian)]
FamilyDependentDep = Annotated[Member, Depends(_family_dependent)]
