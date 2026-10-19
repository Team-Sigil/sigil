"""Logging for the agent service: the same line format as the gateway and the signer.

UTC time to the millisecond, level, logger, message; uvicorn's own lines go through it too
(run uvicorn with ``log_config=None``). Docker's ``GET /healthz`` polls are dropped, and the
platform tokens and model keys are replaced before anything is written.
"""

from __future__ import annotations

import logging
import time

from agent_layer.config import Settings

REDACTED = "[REDACTED]"
LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
HEALTH_PATH = "/healthz"


class RedactSecretsFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


class DropHealthChecksFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access" or not isinstance(record.args, tuple):
            return True
        # uvicorn's access record: (client, method, path, http version, status).
        return len(record.args) < 3 or record.args[2] != HEALTH_PATH


def secret_values(settings: Settings, anthropic_api_key: str = "") -> list[str]:
    """Everything in the agent's settings that must never reach a log line."""
    return [
        settings.maker_token,
        settings.auditor_token,
        settings.inbound_token,
        settings.openrouter_api_key,
        anthropic_api_key,
    ]


def configure_logging(secrets: list[str], level: int = logging.INFO) -> None:
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(DropHealthChecksFilter())
    handler.addFilter(RedactSecretsFilter(secrets))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # The SDKs log every request at INFO; the warnings below say what failed and why.
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
