from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.agent_api.routes import router as agent_router
from gateway.contracts.crypto import CryptoClient
from gateway.core.config import Settings, get_settings
from gateway.core.errors import install_error_handlers
from gateway.core.logging import configure_logging
from gateway.crypto_client import HttpCryptoClient
from gateway.db.session import make_sessionmaker
from gateway.engine.transfers import TransferEngine


def create_app(
    *,
    settings: Settings | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    crypto: CryptoClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.secret_values())
    sessionmaker = sessionmaker or make_sessionmaker(settings.database_url)
    owned_client = None
    if crypto is None:
        owned_client = HttpCryptoClient(
            settings.crypto_url, settings.crypto_hmac_secret.get_secret_value()
        )
        crypto = owned_client

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_client is not None:
            await owned_client.aclose()

    app = FastAPI(title="Sigil gateway", version="0.1.0", lifespan=lifespan)
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
