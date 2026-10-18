"""Sign-In With Solana (SPEC §16.1, SEC-09) and nonces for every guardian signature."""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from gateway.app_api import nonces
from gateway.app_api.sessions import create_token, resolve
from gateway.contracts.app import Pubkey, SignatureB58
from gateway.core.config import Settings
from gateway.core.crypto import verify_ed25519
from gateway.core.errors import UnauthorizedError
from gateway.db import audit_log
from gateway.db.models import User
from gateway.db.session import SessionDep
from gateway.enums import AuditEvent, NoncePurpose, Role

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

NONCE_LINE = re.compile(r"^Nonce: (\S+)$", re.MULTILINE)


class NonceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wallet: Pubkey
    purpose: NoncePurpose


class NonceResponse(BaseModel):
    nonce: str
    message: str | None  # the exact sign-in text for `login`; other purposes use SPEC §8.4
    expires_at: datetime


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wallet: Pubkey
    message: str = Field(min_length=1, max_length=1_000)
    signature_b58: SignatureB58


class TokenResponse(BaseModel):
    token: str
    role: Role
    family_id: str | None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def siws_message(domain: str, wallet: str, nonce: str, expires_at: datetime) -> str:
    """The sign-in text, rebuilt from our own records at verification: bound to our domain,
    the wallet, a single-use nonce and an expiry."""
    return (
        f"{domain} wants you to sign in with your Solana account:\n"
        f"{wallet}\n\n"
        "Sign in to Sigil.\n\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {_iso(expires_at - nonces.NONCE_TTL)}\n"
        f"Expiration Time: {_iso(expires_at)}"
    )


@router.post("/nonce")
async def issue_nonce(body: NonceRequest, request: Request, session: SessionDep) -> NonceResponse:
    settings: Settings = request.app.state.settings
    nonce = await nonces.issue(session, body.wallet, body.purpose, datetime.now(UTC))
    message = None
    if body.purpose == NoncePurpose.LOGIN:
        message = siws_message(settings.siws_domain, body.wallet, nonce.nonce, nonce.expires_at)
    return NonceResponse(nonce=nonce.nonce, message=message, expires_at=nonce.expires_at)


@router.post("/verify")
async def verify(body: VerifyRequest, request: Request, session: SessionDep) -> TokenResponse:
    settings: Settings = request.app.state.settings
    now = datetime.now(UTC)
    match = NONCE_LINE.search(body.message)
    if match is None:
        raise UnauthorizedError("Not a sign-in message.")
    nonce = await nonces.consume(session, body.wallet, NoncePurpose.LOGIN, match.group(1), now)
    expected = siws_message(settings.siws_domain, body.wallet, nonce.nonce, nonce.expires_at)
    if body.message != expected or not verify_ed25519(
        body.wallet, body.message, body.signature_b58
    ):
        raise UnauthorizedError("Signature does not match the sign-in message.")

    user = await session.scalar(select(User).where(User.wallet_address == body.wallet))
    if user is None:
        user = User(wallet_address=body.wallet, role=Role.GUARDIAN)
        session.add(user)
        await session.flush()
    principal = await resolve(session, user)
    await audit_log.append(
        session,
        principal.family.id if principal.family else None,
        AuditEvent.LOGIN,
        {"user_id": str(user.id), "role": principal.role.value},
    )
    await session.commit()
    token = create_token(settings, user, principal.role, principal.family, now)
    return TokenResponse(
        token=token,
        role=principal.role,
        family_id=str(principal.family.id) if principal.family else None,
    )
