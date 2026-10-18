"""Device enrolment, capture tickets and capture verification (agent layer proof-images.md,
"Verified in-app capture").

A capture becomes `source: "photo"` only if, in order: the key is registered and attested for
this dependent; the signature over the claim is valid; the ticket is unused, unexpired and for
this request and device; the SHA-256 of the received bytes equals the claim; and `captured_at`
is inside the ticket's window. Any failure leaves the upload as `source: "file"`.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.capture import android, apple
from gateway.capture.config import CaptureConfig
from gateway.capture.errors import AttestationError
from gateway.core.errors import ConflictError, DomainError, NotFoundError
from gateway.db import audit_log
from gateway.db.models import CaptureDevice, CaptureTicket, Family, Request, User
from gateway.enums import AuditEvent, DevicePlatform

logger = logging.getLogger(__name__)

TICKET_TTL = timedelta(minutes=5)


class AttestationFailedError(DomainError):
    code = "ATTESTATION_FAILED"
    status = 422
    message = "This device could not be verified. Receipts from it will count as plain uploads."


class CaptureClaim(BaseModel):
    """What the app signs with the device key at the moment of capture. The signature covers
    the exact UTF-8 bytes of the claim as uploaded."""

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    captured_at: datetime
    device_key_id: str = Field(min_length=1, max_length=100)
    app_version: str = Field(min_length=1, max_length=40)


@dataclass(frozen=True)
class VerifiedCapture:
    device_id: uuid.UUID
    captured_at: datetime


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestationError("not base64") from exc


async def _store_device(
    session: AsyncSession,
    family: Family,
    user: User,
    platform: DevicePlatform,
    key_id: str,
    public_key: bytes,
    security_level: str | None,
) -> CaptureDevice:
    device = CaptureDevice(
        family_id=family.id,
        user_id=user.id,
        platform=platform,
        key_id=key_id,
        public_key=public_key,
        security_level=security_level,
    )
    session.add(device)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("This device key is already enrolled.") from exc
    await audit_log.append(
        session,
        family.id,
        AuditEvent.DEVICE_ENROLLED,
        {"device_id": str(device.id), "platform": platform.value, "security_level": security_level},
    )
    await session.commit()
    return device


async def enrol_ios(
    session: AsyncSession,
    config: CaptureConfig,
    family: Family,
    user: User,
    *,
    challenge: str,
    key_id_b64: str,
    attestation_b64: str,
    now: datetime,
) -> CaptureDevice:
    try:
        if not config.apple_app_id:
            raise AttestationError("App Attest is not configured")
        enrolled = apple.verify_attestation(
            attestation_object=_b64(attestation_b64),
            key_id=_b64(key_id_b64),
            challenge=challenge.encode(),
            app_id=config.apple_app_id,
            production=config.apple_production,
            roots=config.apple_roots,
            now=now,
        )
    except AttestationError as exc:
        logger.warning("iOS enrolment refused: %s", exc)
        raise AttestationFailedError() from exc
    return await _store_device(
        session,
        family,
        user,
        DevicePlatform.IOS,
        key_id_b64,
        enrolled.public_key_der,
        "secure_enclave",
    )


async def enrol_android(
    session: AsyncSession,
    config: CaptureConfig,
    family: Family,
    user: User,
    *,
    challenge: str,
    chain_b64: list[str],
    integrity_token: str,
    now: datetime,
) -> CaptureDevice:
    try:
        if not config.android_package or not config.android_signing_digests:
            raise AttestationError("Android attestation is not configured")
        enrolled = android.verify_key_attestation(
            chain=[_b64(c) for c in chain_b64],
            challenge=challenge.encode(),
            package=config.android_package,
            signing_digests=config.android_signing_digests,
            roots=config.google_roots,
            now=now,
        )
        await config.integrity.verify(integrity_token, challenge, int(now.timestamp() * 1000))
    except AttestationError as exc:
        logger.warning("Android enrolment refused: %s", exc)
        raise AttestationFailedError() from exc
    key_id = hashlib.sha256(enrolled.public_key_der).hexdigest()
    return await _store_device(
        session,
        family,
        user,
        DevicePlatform.ANDROID,
        key_id,
        enrolled.public_key_der,
        enrolled.security_level,
    )


async def family_device(
    session: AsyncSession, family: Family, device_id: str, user: User | None = None
) -> CaptureDevice:
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError as exc:
        raise NotFoundError("No such device.") from exc
    conditions = [CaptureDevice.id == device_uuid, CaptureDevice.family_id == family.id]
    if user is not None:
        conditions.append(CaptureDevice.user_id == user.id)
    device = await session.scalar(select(CaptureDevice).where(*conditions))
    if device is None:
        raise NotFoundError("No such device.")
    return device


async def revoke(session: AsyncSession, device: CaptureDevice, now: datetime) -> CaptureDevice:
    if device.revoked_at is None:
        device.revoked_at = now
        await audit_log.append(
            session, device.family_id, AuditEvent.DEVICE_REVOKED, {"device_id": str(device.id)}
        )
        await session.commit()
    return device


async def issue_ticket(
    session: AsyncSession, request: Request, device: CaptureDevice, now: datetime
) -> CaptureTicket:
    if device.revoked_at is not None:
        raise ConflictError("This device was revoked.")
    ticket = CaptureTicket(
        nonce=secrets.token_urlsafe(24),
        request_id=request.id,
        device_id=device.id,
        issued_at=now,
        expires_at=now + TICKET_TTL,
    )
    session.add(ticket)
    await session.commit()
    return ticket


def _verify_signature(
    device: CaptureDevice, claim_bytes: bytes, signature: bytes, config: CaptureConfig
) -> int | None:
    """Returns the new App Attest counter (iOS) or None (Android). Raises on a bad signature."""
    if device.platform == DevicePlatform.IOS:
        if not config.apple_app_id:
            raise AttestationError("App Attest is not configured")
        return apple.verify_assertion(
            assertion=signature,
            client_data=claim_bytes,
            public_key_der=device.public_key,
            stored_counter=device.counter,
            app_id=config.apple_app_id,
        )
    public_key = serialization.load_der_public_key(device.public_key)
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise AttestationError("stored key is not an EC key")
    try:
        public_key.verify(signature, claim_bytes, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise AttestationError("capture signature invalid") from exc
    return None


async def verify_capture(
    session: AsyncSession,
    config: CaptureConfig,
    user: User,
    request: Request,
    data: bytes,
    claim_raw: str,
    signature_b64: str,
    now: datetime,
) -> VerifiedCapture | None:
    """The checks from the module docstring, in order. None means "not verified": the upload is
    kept as a plain file, and the reason is logged. The caller commits (ticket use, counter)."""
    try:
        claim_bytes = claim_raw.encode()
        claim = CaptureClaim.model_validate_json(claim_bytes)
        if claim.request_id != str(request.id):
            raise AttestationError("claim is for another request")

        # 1. The key is registered and attested for this dependent (row locked for the counter).
        device = await session.scalar(
            select(CaptureDevice)
            .where(
                CaptureDevice.key_id == claim.device_key_id,
                CaptureDevice.user_id == user.id,
                CaptureDevice.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if device is None:
            raise AttestationError("device not enrolled for this dependent")

        # 2. The signature over the exact claim bytes is valid.
        counter = _verify_signature(device, claim_bytes, _b64(signature_b64), config)

        # 3. The ticket is unused, unexpired and for this request and device (consumed here).
        ticket: CaptureTicket | None = await session.scalar(
            update(CaptureTicket)
            .where(
                CaptureTicket.nonce == claim.nonce,
                CaptureTicket.request_id == request.id,
                CaptureTicket.device_id == device.id,
                CaptureTicket.used_at.is_(None),
                CaptureTicket.expires_at > now,
            )
            .values(used_at=now)
            .returning(CaptureTicket)
        )
        if ticket is None:
            raise AttestationError("capture ticket invalid, expired or used")

        # 4. The bytes received are the bytes that were signed.
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), claim.sha256):
            raise AttestationError("image does not match the signed hash")

        # 5. Captured while the ticket was valid.
        if not ticket.issued_at <= claim.captured_at <= ticket.expires_at:
            raise AttestationError("captured outside the ticket window")
    except (AttestationError, ValidationError, ValueError) as exc:
        # No rollback: a ticket consumed by a failed capture stays used. The caller commits.
        logger.warning("Capture not verified for request %s: %s", request.id, exc)
        return None

    if counter is not None:
        device.counter = counter
    return VerifiedCapture(device_id=device.id, captured_at=claim.captured_at)
