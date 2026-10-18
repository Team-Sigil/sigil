"""Server nonces for wallet signatures: single use, 5 minutes, bound to a wallet and a purpose
(SPEC SEC-09, §8.4)."""

import secrets
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.errors import UnauthorizedError
from gateway.db.models import Nonce
from gateway.enums import NoncePurpose

NONCE_TTL = timedelta(minutes=5)


async def issue(session: AsyncSession, wallet: str, purpose: NoncePurpose, now: datetime) -> Nonce:
    nonce = Nonce(
        nonce=secrets.token_urlsafe(24),
        wallet=wallet,
        purpose=purpose,
        expires_at=now + NONCE_TTL,
    )
    session.add(nonce)
    await session.commit()
    return nonce


async def consume(
    session: AsyncSession, wallet: str, purpose: NoncePurpose, nonce: str, now: datetime
) -> Nonce:
    """Marks the nonce used in one statement, so two concurrent uses cannot both succeed.
    Callers consume before checking the signature: a failed attempt burns the nonce too."""
    used: Nonce | None = await session.scalar(
        update(Nonce)
        .where(
            Nonce.nonce == nonce,
            Nonce.wallet == wallet,
            Nonce.purpose == purpose,
            Nonce.used_at.is_(None),
            Nonce.expires_at > now,
        )
        .values(used_at=now)
        .returning(Nonce)
    )
    await session.commit()
    if used is None:
        raise UnauthorizedError("Nonce is invalid, expired or already used.")
    return used
