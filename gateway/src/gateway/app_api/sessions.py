"""JWT sessions for the app layer and the role dependencies routes use.

The token only proves who the caller is (`sub` = user id). Role and family are read from the
database on every request, so a token never keeps a role or family the user no longer has.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.config import Settings
from gateway.core.errors import ForbiddenError, UnauthorizedError
from gateway.db.models import Family, User
from gateway.db.session import SessionDep
from gateway.enums import Role

JWT_ALGORITHM = "HS256"


@dataclass(frozen=True)
class Principal:
    user: User
    role: Role
    family: Family | None


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


async def resolve(session: AsyncSession, user: User) -> Principal:
    """A wallet registered as a family's dependent is a dependent; every other wallet is a
    guardian, with or without a family yet."""
    family = await session.scalar(
        select(Family).where(
            or_(Family.guardian_user_id == user.id, Family.dependent_user_id == user.id)
        )
    )
    if family is not None and family.dependent_user_id == user.id:
        return Principal(user=user, role=Role.DEPENDENT, family=family)
    return Principal(user=user, role=Role.GUARDIAN, family=family)


async def current_principal(
    request: Request, session: SessionDep, authorization: str = Header(default="")
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
    return await resolve(session, user)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _member(principal: PrincipalDep) -> Member:
    if principal.family is None:
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
