import logging
import re

from signer.logging import DropHealthChecksFilter, configure_logging


def access_record(path: str) -> logging.LogRecord:
    args = ("172.18.0.1:50000", "POST", path, "1.1", 200)
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "f", 1, '%s - "%s %s HTTP/%s" %d', args, None
    )


def test_health_checks_are_dropped_and_other_requests_kept() -> None:
    drop = DropHealthChecksFilter()
    assert drop.filter(access_record("/healthz")) is False
    assert drop.filter(access_record("/v1/transfer")) is True


def test_log_lines_carry_a_utc_timestamp() -> None:
    root = logging.getLogger()
    saved = root.handlers, root.level
    try:
        configure_logging([])
        line = root.handlers[0].format(access_record("/v1/transfer"))
    finally:
        root.handlers, root.level = saved
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO uvicorn\.access ", line)
