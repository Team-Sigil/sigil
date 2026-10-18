from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.agent_api.routes import router as agent_router
from gateway.contracts.crypto import (
    CryptoClient,
    CryptoTransferRequest,
    CryptoTransferResponse,
    CryptoUnavailableError,
    Delegation,
)
from gateway.core.config import Settings, get_settings
from gateway.core.errors import install_error_handlers
from gateway.core.logging import configure_logging
from gateway.db.session import make_sessionmaker
from gateway.engine.transfers import TransferEngine


class NoCrypto:
    """Fails closed until the crypto layer client is configured: no delegation, no transfers."""

    async def get_delegation(self, token_account: str) -> Delegation:
        raise CryptoUnavailableError("crypto layer not configured")

    async def transfer(self, request: CryptoTransferRequest) -> CryptoTransferResponse:
        raise CryptoUnavailableError("crypto layer not configured")


def create_app(
    *,
    settings: Settings | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    crypto: CryptoClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.secret_values())
    sessionmaker = sessionmaker or make_sessionmaker(settings.database_url)
    crypto = crypto or NoCrypto()

    app = FastAPI(title="Sigil gateway", version="0.1.0")
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.crypto = crypto
    app.state.engine = TransferEngine(
        sessionmaker,
        crypto,
        agent_wallet=settings.agent_wallet_pubkey,
        mint=settings.ausd_mint,
    )
    install_error_handlers(app)
    app.include_router(agent_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
