"""HTTP client for the crypto layer (see `contracts/crypto.py` and README).

Every request is HMAC-signed: `X-Signature` = hex HMAC-SHA256(secret,
`{timestamp}.{nonce}.{raw_body}`), with `X-Timestamp` (unix seconds) and `X-Nonce` (UUID).
The crypto layer rejects a bad signature, a timestamp off by more than 30 s, or a reused
nonce.
"""

import hashlib
import hmac
import re
import time
import uuid

import httpx
from pydantic import ValidationError

from gateway.contracts.crypto import (
    CryptoTransferRequest,
    CryptoTransferResponse,
    CryptoTransferStatus,
    CryptoUnavailableError,
    Delegation,
)

MAX_RESPONSE_BYTES = 64 * 1024
MAX_CLOCK_SKEW_SECONDS = 30
TOKEN_ACCOUNT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = f"{timestamp}.{nonce}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, body: bytes) -> dict[str, str]:
    timestamp, nonce = str(int(time.time())), str(uuid.uuid4())
    return {
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": sign(secret, timestamp, nonce, body),
    }


class HttpCryptoClient:
    def __init__(
        self,
        base_url: str,
        hmac_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._secret = hmac_secret
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_delegation(self, token_account: str) -> Delegation:
        if not TOKEN_ACCOUNT.match(token_account):
            raise CryptoUnavailableError("invalid token account")
        try:
            response = await self._http.get(
                f"/v1/delegation/{token_account}", headers=signed_headers(self._secret, b"")
            )
        except httpx.HTTPError as exc:
            raise CryptoUnavailableError(f"transport failure: {type(exc).__name__}") from exc
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            raise CryptoUnavailableError(f"delegation read failed ({response.status_code})")
        try:
            delegation = Delegation.model_validate_json(response.content)
        except ValidationError as exc:
            raise CryptoUnavailableError("delegation outside the contract") from exc
        if delegation.token_account != token_account:
            raise CryptoUnavailableError("delegation for another account")
        return delegation

    async def transfer(self, request: CryptoTransferRequest) -> CryptoTransferResponse:
        body = request.model_dump_json().encode()
        headers = signed_headers(self._secret, body) | {"Content-Type": "application/json"}
        try:
            response = await self._http.post("/v1/transfer", content=body, headers=headers)
        except httpx.HTTPError as exc:
            # The request may or may not have reached the signer: the outcome is unknown.
            raise CryptoUnavailableError(f"transport failure: {type(exc).__name__}") from exc
        if 400 <= response.status_code < 500:
            # Refused before signing (bad HMAC, unknown destination, over its caps): definitely
            # not sent, so the engine can release the reservation.
            return CryptoTransferResponse(
                status=CryptoTransferStatus.FAILED, error_code=_error_code(response)
            )
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            raise CryptoUnavailableError(f"transfer outcome unknown ({response.status_code})")
        try:
            return CryptoTransferResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise CryptoUnavailableError("transfer response outside the contract") from exc


def _error_code(response: httpx.Response) -> str:
    try:
        code = response.json().get("error_code")
    except ValueError:
        code = None
    return code if isinstance(code, str) and len(code) <= 64 else "SIGNER_REJECTED"
