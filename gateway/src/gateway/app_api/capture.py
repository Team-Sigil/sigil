"""Verified in-app capture: enrol the dependent's phone, then get a ticket per receipt.

1. `POST /capture/challenge`, then `POST /capture/devices` with the platform attestation.
2. For each receipt: `POST /requests/{id}/capture-tickets`, take the photo in the app, sign the
   claim with the device key, upload it with `capture_claim` and `capture_signature`.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from gateway.app_api import nonces
from gateway.app_api.requests import family_request
from gateway.app_api.sessions import FamilyDependentDep, MemberDep
from gateway.capture import service
from gateway.capture.config import CaptureConfig
from gateway.db.models import CaptureDevice
from gateway.db.session import SessionDep
from gateway.enums import NoncePurpose, Role

router = APIRouter(prefix="/api/v1", tags=["capture"])

Base64 = Annotated[str, Field(min_length=1, max_length=16_000)]


class Challenge(BaseModel):
    challenge: str
    expires_at: datetime


class IosEnrolment(BaseModel):
    """`key_id` and `attestation_object` from `DCAppAttestService`, base64. The app attests
    with clientDataHash = SHA-256(UTF-8 challenge)."""

    model_config = ConfigDict(extra="forbid")

    platform: Literal["ios"]
    challenge: str = Field(min_length=1, max_length=64)
    key_id: Base64
    attestation_object: Base64


class AndroidEnrolment(BaseModel):
    """The key's attestation chain (leaf first, base64 DER), generated with the UTF-8
    challenge as the attestation challenge, and a classic Play Integrity token requested with
    the challenge as its nonce."""

    model_config = ConfigDict(extra="forbid")

    platform: Literal["android"]
    challenge: str = Field(min_length=1, max_length=64)
    certificate_chain: list[Base64] = Field(min_length=1, max_length=10)
    integrity_token: str = Field(min_length=1, max_length=16_000)


class DeviceView(BaseModel):
    id: str
    platform: str
    key_id: str
    security_level: str | None
    created_at: datetime
    revoked_at: datetime | None


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(max_length=64)


class Ticket(BaseModel):
    ticket: str  # the claim's `nonce`
    request_id: str
    device_id: str
    expires_at: datetime


def _device_view(device: CaptureDevice) -> DeviceView:
    return DeviceView(
        id=str(device.id),
        platform=device.platform.value,
        key_id=device.key_id,
        security_level=device.security_level,
        created_at=device.created_at,
        revoked_at=device.revoked_at,
    )


def _config(request: Request) -> CaptureConfig:
    config: CaptureConfig = request.app.state.capture
    return config


@router.post("/capture/challenge")
async def challenge(dependent: FamilyDependentDep, session: SessionDep) -> Challenge:
    nonce = await nonces.issue(
        session, dependent.user.wallet_address, NoncePurpose.ENROL_DEVICE, datetime.now(UTC)
    )
    return Challenge(challenge=nonce.nonce, expires_at=nonce.expires_at)


@router.post("/capture/devices")
async def enrol(
    body: Annotated[IosEnrolment | AndroidEnrolment, Field(discriminator="platform")],
    dependent: FamilyDependentDep,
    request: Request,
    session: SessionDep,
) -> DeviceView:
    now = datetime.now(UTC)
    # Single use, bound to this dependent's wallet, burned even if attestation then fails.
    await nonces.consume(
        session, dependent.user.wallet_address, NoncePurpose.ENROL_DEVICE, body.challenge, now
    )
    if isinstance(body, IosEnrolment):
        device = await service.enrol_ios(
            session,
            _config(request),
            dependent.family,
            dependent.user,
            challenge=body.challenge,
            key_id_b64=body.key_id,
            attestation_b64=body.attestation_object,
            now=now,
        )
    else:
        device = await service.enrol_android(
            session,
            _config(request),
            dependent.family,
            dependent.user,
            challenge=body.challenge,
            chain_b64=body.certificate_chain,
            integrity_token=body.integrity_token,
            now=now,
        )
    return _device_view(device)


@router.get("/capture/devices")
async def list_devices(member: MemberDep, session: SessionDep) -> list[DeviceView]:
    rows = await session.scalars(
        select(CaptureDevice)
        .where(CaptureDevice.family_id == member.family.id)
        .order_by(CaptureDevice.created_at)
    )
    return [_device_view(d) for d in rows]


@router.delete("/capture/devices/{device_id}")
async def revoke_device(device_id: str, member: MemberDep, session: SessionDep) -> DeviceView:
    """The dependent can revoke their own phone; the guardian can revoke any family device."""
    owner = member.user if member.role == Role.DEPENDENT else None
    device = await service.family_device(session, member.family, device_id, owner)
    return _device_view(await service.revoke(session, device, datetime.now(UTC)))


@router.post("/requests/{request_id}/capture-tickets")
async def capture_ticket(
    request_id: str, body: TicketRequest, dependent: FamilyDependentDep, session: SessionDep
) -> Ticket:
    target = await family_request(session, dependent, request_id)
    device = await service.family_device(session, dependent.family, body.device_id, dependent.user)
    ticket = await service.issue_ticket(session, target, device, datetime.now(UTC))
    return Ticket(
        ticket=ticket.nonce,
        request_id=str(target.id),
        device_id=str(device.id),
        expires_at=ticket.expires_at,
    )
