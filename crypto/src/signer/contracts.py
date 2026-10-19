"""Gateway ↔ crypto layer (Han). Defined here; see README "Crypto layer contract".

`POST /v1/transfer` sends a cleared transfer; `GET /v1/delegation/{token_account}` is the
engine's on-chain delegation read (owner, delegate, remaining amount). See SPEC §7.4, §9.
Every request carries `X-Timestamp`, `X-Nonce` and `X-Signature` (HMAC-SHA256 over
`{timestamp}.{nonce}.{raw_body}`, empty body for GET).
"""

from enum import StrEnum
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CryptoTransferRequest(BaseModel):
    """A cleared transfer for the signer. The destination is the family's registered token
    account, resolved by the engine, never by the agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transfer_id: UUID
    family_id: UUID
    amount_units: Annotated[int, Field(strict=True, gt=0)]
    source_token_account: str = Field(max_length=44)
    destination_token_account: str = Field(max_length=44)
    mint: str = Field(max_length=44)
    idempotency_key: str = Field(max_length=64)


class CryptoTransferStatus(StrEnum):
    CONFIRMED = "confirmed"
    FAILED = "failed"


class CryptoTransferResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: CryptoTransferStatus
    tx_signature: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=64)


class Delegation(BaseModel):
    """The SPL token account and its delegation, read from chain. `owner` lets the engine
    check the account really is the guardian's before spending from it."""

    model_config = ConfigDict(frozen=True)

    token_account: str = Field(max_length=44)
    owner: str = Field(max_length=44)
    delegate: str | None = Field(default=None, max_length=44)
    delegated_amount_units: Annotated[int, Field(strict=True, ge=0)]


class CryptoUnavailableError(Exception):
    """The crypto layer could not be reached, refused the request, or answered outside this
    contract. The outcome of a transfer call is then unknown."""


class CryptoClient(Protocol):
    async def get_delegation(self, token_account: str) -> Delegation: ...

    async def transfer(self, request: CryptoTransferRequest) -> CryptoTransferResponse: ...
