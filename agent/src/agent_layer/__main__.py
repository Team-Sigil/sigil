"""Run the agent service: ``python -m agent_layer``.

Environment: PLATFORM_API_URL, PLATFORM_MAKER_TOKEN, PLATFORM_AUDITOR_TOKEN,
AGENT_INBOUND_TOKEN, and the model credentials: ANTHROPIC_API_KEY (or another SDK
credential source) for Claude, OPENROUTER_API_KEY with AGENT_LLM_PROVIDER=openrouter.
Optionally AGENT_HOST / AGENT_PORT / AGENT_SCHEDULER=0.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn

from agent_layer.config import Settings
from agent_layer.log_setup import configure_logging, secret_values
from agent_layer.sandbox import install_sandbox
from agent_layer.scheduler import build_scheduler
from agent_layer.service import build_runtime, create_app


def allowed_hosts(settings: Settings) -> list[str]:
    """The platform, plus the API host of each model provider in use, and nothing else."""
    hosts = [urlparse(settings.platform_base_url).hostname or ""]
    if "anthropic" in settings.providers:
        hosts.append(urlparse(os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).hostname or "")
    if "openrouter" in settings.providers:
        hosts.append(urlparse(settings.openrouter_base_url).hostname or "")
    return [h for h in hosts if h]


def main() -> None:
    settings = Settings.from_env()
    configure_logging(secret_values(settings, os.environ.get("ANTHROPIC_API_KEY", "")))
    install_sandbox(allowed_hosts=allowed_hosts(settings), allow_listen=True)

    runtime = build_runtime(settings)
    scheduler = build_scheduler(runtime.jobs) if os.environ.get("AGENT_SCHEDULER", "1") != "0" else None

    app = create_app(runtime)

    @asynccontextmanager
    async def lifespan(_app):
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host=os.environ.get("AGENT_HOST", "127.0.0.1"), port=int(os.environ.get("AGENT_PORT", "8100")), log_config=None)


if __name__ == "__main__":
    main()
