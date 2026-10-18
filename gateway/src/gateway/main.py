from fastapi import FastAPI

from gateway.core.config import get_settings
from gateway.core.errors import install_error_handlers
from gateway.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.secret_values())

    app = FastAPI(title="Sigil gateway", version="0.1.0")
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
