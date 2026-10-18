from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.agent_api.routes import router as agent_router
from gateway.agent_client import AgentService, HttpAgentClient
from gateway.app_api.auth import router as auth_router
from gateway.app_api.capture import router as capture_router
from gateway.app_api.families import router as families_router
from gateway.app_api.guardian import router as guardian_router
from gateway.app_api.policy import router as policy_router
from gateway.app_api.reads import router as reads_router
from gateway.app_api.requests import router as requests_router
from gateway.capture.config import CaptureConfig, capture_config
from gateway.capture.play_integrity import GooglePlayIntegrity
from gateway.contracts.crypto import CryptoClient
from gateway.core.config import Settings, get_settings
from gateway.core.errors import install_error_handlers
from gateway.core.logging import configure_logging
from gateway.crypto_client import HttpCryptoClient
from gateway.db.session import make_sessionmaker
from gateway.engine.transfers import TransferEngine
from gateway.events import EventBus
from gateway.protection import BodyLimitMiddleware, RateLimitMiddleware


def create_app(
    *,
    settings: Settings | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    crypto: CryptoClient | None = None,
    agent: AgentService | None = None,
    capture: CaptureConfig | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.secret_values())
    sessionmaker = sessionmaker or make_sessionmaker(settings.database_url)
    owned_crypto = owned_agent = None
    if crypto is None:
        owned_crypto = HttpCryptoClient(
            settings.crypto_url, settings.crypto_hmac_secret.get_secret_value()
        )
        crypto = owned_crypto
    if agent is None:
        owned_agent = HttpAgentClient(
            settings.agent_url, settings.agent_inbound_token.get_secret_value()
        )
        agent = owned_agent
    capture = capture or capture_config(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_crypto is not None:
            await owned_crypto.aclose()
        if owned_agent is not None:
            await owned_agent.aclose()
        if isinstance(capture.integrity, GooglePlayIntegrity):
            await capture.integrity.aclose()

    app = FastAPI(title="Sigil gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.crypto = crypto
    app.state.agent = agent
    app.state.events = EventBus()
    app.state.capture = capture
    app.state.engine = TransferEngine(
        sessionmaker,
        crypto,
        agent_wallet=settings.agent_wallet_pubkey,
        mint=settings.ausd_mint,
        camera_only=settings.camera_only_proofs,
    )
    install_error_handlers(app)
    app.include_router(agent_router)
    app.include_router(auth_router)
    app.include_router(families_router)
    app.include_router(policy_router)
    app.include_router(requests_router)
    app.include_router(guardian_router)
    app.include_router(reads_router)
    app.include_router(capture_router)
    # Added last so they run first: size, then rate, before any route work.
    app.add_middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_min)
    app.add_middleware(BodyLimitMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
