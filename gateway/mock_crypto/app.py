"""In-memory mock of the crypto layer's API, for local development and tests, and a reference
for the real signer (Han). It checks the HMAC, timestamp and nonce like the real one, keeps
delegations per token account, and is idempotent on the transfer's key. It never touches a
chain: signatures are `mock_<uuid>`.
"""

import hmac
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway.contracts.crypto import (
    CryptoTransferRequest,
    CryptoTransferResponse,
    CryptoTransferStatus,
    Delegation,
)
from gateway.crypto_client import MAX_CLOCK_SKEW_SECONDS, sign

NONCE_TTL_SECONDS = 600


class DelegationUpdate(BaseModel):
    owner: str
    delegate: str | None
    delegated_amount_units: int


@dataclass
class MockCrypto:
    secret: str
    admin_token: str = "mock-crypto-admin"  # noqa: S105 - mock-only admin routes
    delegations: dict[str, Delegation] = field(default_factory=dict)
    transfers: dict[str, CryptoTransferResponse] = field(default_factory=dict)
    sent: list[CryptoTransferRequest] = field(default_factory=list)
    nonces: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def verify(self, timestamp: str, nonce: str, signature: str, body: bytes) -> bool:
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
            return False
        if not hmac.compare_digest(signature, sign(self.secret, timestamp, nonce, body)):
            return False
        with self.lock:
            now = time.time()
            self.nonces = {n: t for n, t in self.nonces.items() if now - t < NONCE_TTL_SECONDS}
            if nonce in self.nonces:
                return False
            self.nonces[nonce] = now
        return True

    def transfer(self, req: CryptoTransferRequest) -> CryptoTransferResponse:
        with self.lock:
            if req.idempotency_key in self.transfers:
                return self.transfers[req.idempotency_key]
            delegation = self.delegations.get(req.source_token_account)
            if delegation is None or delegation.delegated_amount_units < req.amount_units:
                result = CryptoTransferResponse(
                    status=CryptoTransferStatus.FAILED, error_code="INSUFFICIENT_DELEGATION"
                )
            else:
                self.delegations[req.source_token_account] = delegation.model_copy(
                    update={
                        "delegated_amount_units": delegation.delegated_amount_units
                        - req.amount_units
                    }
                )
                self.sent.append(req)
                result = CryptoTransferResponse(
                    status=CryptoTransferStatus.CONFIRMED, tx_signature=f"mock_{uuid.uuid4().hex}"
                )
            self.transfers[req.idempotency_key] = result
            return result


def create_mock_app(mock: MockCrypto) -> FastAPI:
    app = FastAPI(title="Mock crypto layer")

    async def verified_body(request: Request) -> bytes:
        body = await request.body()
        headers = request.headers
        if not mock.verify(
            headers.get("x-timestamp", ""),
            headers.get("x-nonce", ""),
            headers.get("x-signature", ""),
            body,
        ):
            raise HTTPException(status_code=401, detail={"error_code": "SIGNER_REJECTED"})
        return body

    @app.exception_handler(HTTPException)
    async def flat_errors(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error_code": "ERROR"}
        return JSONResponse(status_code=exc.status_code, content=detail)

    @app.get("/v1/delegation/{token_account}")
    async def get_delegation(token_account: str, request: Request) -> Delegation:
        await verified_body(request)
        delegation = mock.delegations.get(token_account)
        if delegation is None:
            raise HTTPException(status_code=404, detail={"error_code": "UNKNOWN_ACCOUNT"})
        return delegation

    @app.post("/v1/transfer")
    async def transfer(request: Request) -> CryptoTransferResponse:
        body = await verified_body(request)
        return mock.transfer(CryptoTransferRequest.model_validate_json(body))

    # ---- mock-only admin routes: stand in for the guardian's `approve` / `revoke` in Phantom.
    @app.put("/mock/delegations/{token_account}")
    async def set_delegation(
        token_account: str, body: DelegationUpdate, authorization: str = Header("")
    ) -> Delegation:
        if not hmac.compare_digest(authorization, f"Bearer {mock.admin_token}"):
            raise HTTPException(status_code=401, detail={"error_code": "ADMIN_TOKEN_REQUIRED"})
        delegation = Delegation(token_account=token_account, **body.model_dump())
        mock.delegations[token_account] = delegation
        return delegation

    return app
