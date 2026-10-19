import logging
import time

REDACTED = "[REDACTED]"
LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
HEALTH_PATH = "/healthz"


class RedactSecretsFilter(logging.Filter):
    """Replaces known secret values in log records before output (SEC-13)."""

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
    """Drops uvicorn's access line for `GET /healthz`: Docker polls it, and it buries
    the requests that matter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access" or not isinstance(record.args, tuple):
            return True
        # uvicorn's access record: (client, method, path, http version, status).
        return len(record.args) < 3 or record.args[2] != HEALTH_PATH


def configure_logging(secrets: list[str], level: int = logging.INFO) -> None:
    """One format for everything, uvicorn's own lines included (run it with
    `log_config=None`): UTC time to the millisecond, level, logger, message."""
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(DropHealthChecksFilter())
    handler.addFilter(RedactSecretsFilter(secrets))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO, and an RPC URL can carry an API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
