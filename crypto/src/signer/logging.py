import logging

REDACTED = "[REDACTED]"


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


def configure_logging(secrets: list[str], level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactSecretsFilter(secrets))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO, and an RPC URL can carry an API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
