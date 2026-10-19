"""The signer's HTTP API: `GET /v1/delegation/{token_account}` and `POST /v1/transfer`, both
HMAC-authenticated (see `auth.py`). The gateway is the only client.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from solders.pubkey import Pubkey

from signer.auth import verify_request
from signer.chain import DEVNET_GENESIS_HASH, ChainUnavailableError, SolanaRpc
from signer.config import Settings
from signer.contracts import CryptoTransferRequest, CryptoTransferResponse, Delegation
from signer.keys import AgentWallet
from signer.logging import configure_logging
from signer.registry import PostgresRegistry, Registry, RegistryUnavailableError
from signer.store import SignerStore
from signer.transfers import OutcomeUnknownError, Signer, SignerLimits, TransferRefusedError

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024


class SignerHTTPError(Exception):
    def __init__(self, status_code: int, error_code: str) -> None:
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code


class StartupRefusedError(Exception):
    """The signer refuses to run with this configuration."""


def create_app(
    settings: Settings,
    *,
    registry: Registry | None = None,
    rpc: SolanaRpc | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    configure_logging(settings.secret_values())
    # Fails here, before anything listens, if the key doesn't decrypt or doesn't derive to
    # AGENT_WALLET_PUBKEY (SEC-13). The devnet check already ran in Settings (SEC-14).
    wallet = AgentWallet.from_encrypted(
        settings.agent_wallet_key_enc.get_secret_value(),
        settings.signer_master_key.get_secret_value(),
        settings.agent_wallet_pubkey,
    )
    rpc = rpc or SolanaRpc(settings.solana_rpc_url)
    postgres = None
    if registry is None:
        postgres = PostgresRegistry(settings.database_url_readonly.get_secret_value())
        registry = postgres
    store = SignerStore(settings.signer_db_path)
    signer = Signer(
        store=store,
        registry=registry,
        rpc=rpc,
        wallet=wallet,
        limits=SignerLimits(
            ausd_mint=settings.ausd_mint,
            max_per_transfer_units=settings.signer_max_per_transfer_units,
            max_per_day_units=settings.signer_max_per_day_units,
            max_per_source_per_day_units=settings.signer_max_per_source_per_day_units,
        ),
        mock_chain=settings.mock_chain,
        confirm_timeout_seconds=settings.confirm_timeout_seconds,
        clock=clock,
        sleep=sleep,
    )
    hmac_secret = settings.signer_hmac_secret.get_secret_value()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not settings.mock_chain:
            # SEC-14 beyond the URL: the cluster itself must be devnet.
            genesis = await rpc.get_genesis_hash()
            if genesis != DEVNET_GENESIS_HASH:
                raise StartupRefusedError("SOLANA_RPC_URL is not Solana devnet")
        if postgres is not None:
            await postgres.open()
        logger.info(
            "Signer ready: agent wallet %s, mock chain %s", wallet.pubkey, settings.mock_chain
        )
        try:
            yield
        finally:
            if postgres is not None:
                await postgres.aclose()
            await rpc.aclose()
            store.close()

    app = FastAPI(title="Sigil signer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.signer = signer

    async def verified_body(request: Request) -> bytes:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise SignerHTTPError(413, "REQUEST_TOO_LARGE")
        headers = request.headers
        if not verify_request(
            store,
            hmac_secret,
            headers.get("x-timestamp", ""),
            headers.get("x-nonce", ""),
            headers.get("x-signature", ""),
            body,
            clock(),
        ):
            raise SignerHTTPError(401, "SIGNER_REJECTED")
        return body

    def get_signer(request: Request) -> Signer:
        signer: Signer = request.app.state.signer
        return signer

    VerifiedBody = Annotated[bytes, Depends(verified_body)]  # noqa: N806
    SignerDep = Annotated[Signer, Depends(get_signer)]  # noqa: N806

    @app.exception_handler(SignerHTTPError)
    async def http_error(_: Request, exc: SignerHTTPError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error_code": exc.error_code})

    @app.exception_handler(TransferRefusedError)
    async def refused(_: Request, exc: TransferRefusedError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error_code": exc.error_code})

    @app.exception_handler(OutcomeUnknownError)
    async def unknown(_: Request, exc: OutcomeUnknownError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error_code": exc.error_code})

    @app.exception_handler(ChainUnavailableError)
    async def chain_unavailable(_: Request, exc: ChainUnavailableError) -> JSONResponse:
        logger.warning("Chain unavailable: %s", exc)
        return JSONResponse(status_code=503, content={"error_code": "CHAIN_UNAVAILABLE"})

    @app.exception_handler(RegistryUnavailableError)
    async def registry_unavailable(_: Request, exc: RegistryUnavailableError) -> JSONResponse:
        logger.warning("Registry unavailable: %s", exc)
        return JSONResponse(status_code=503, content={"error_code": "REGISTRY_UNAVAILABLE"})

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 503, never 4xx: for a transfer, an unexpected error means "outcome unknown".
        logger.exception("Unexpected error")
        return JSONResponse(status_code=503, content={"error_code": "SIGNER_ERROR"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/delegation/{token_account}")
    async def get_delegation(
        token_account: str, _body: VerifiedBody, signer: SignerDep
    ) -> Delegation:
        try:
            Pubkey.from_string(token_account)
        except ValueError:
            raise SignerHTTPError(404, "UNKNOWN_ACCOUNT") from None
        delegation = await signer.get_delegation(token_account)
        if delegation is None:
            raise SignerHTTPError(404, "UNKNOWN_ACCOUNT")
        return delegation

    @app.post("/v1/transfer")
    async def transfer(body: VerifiedBody, signer: SignerDep) -> CryptoTransferResponse:
        try:
            req = CryptoTransferRequest.model_validate_json(body)
        except ValidationError:
            raise SignerHTTPError(422, "INVALID_REQUEST") from None
        return await signer.transfer(req)

    return app
