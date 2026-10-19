"""The agent's log lines: UTC timestamps, no health-check noise, no secrets."""

from __future__ import annotations

import logging
import re

import pytest

from agent_layer.config import Settings
from agent_layer.log_setup import REDACTED, DropHealthChecksFilter, configure_logging, secret_values


def access_record(path: str) -> logging.LogRecord:
    args = ("172.18.0.4:50000", "POST", path, "1.1", 200)
    return logging.LogRecord("uvicorn.access", logging.INFO, "f", 1, '%s - "%s %s HTTP/%s" %d', args, None)


@pytest.fixture
def root_handler():
    """configure_logging() replaces the root handlers; put pytest's back afterwards."""
    root = logging.getLogger()
    saved = root.handlers, root.level
    settings = Settings(platform_base_url="http://gateway:8000", maker_token="maker-secret-token",
                        auditor_token="auditor-secret-token", inbound_token="inbound-secret-token",
                        openrouter_api_key="sk-or-secret-key")
    configure_logging(secret_values(settings, "sk-ant-secret-key"))
    yield root.handlers[0]
    root.handlers, root.level = saved


def test_health_checks_are_dropped_and_other_requests_kept():
    drop = DropHealthChecksFilter()
    assert drop.filter(access_record("/healthz")) is False
    assert drop.filter(access_record("/v1/policy-interviews/turn")) is True


def test_lines_carry_a_utc_timestamp(root_handler):
    line = root_handler.format(access_record("/v1/policy-interviews/turn"))
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO uvicorn\.access ", line)


@pytest.mark.parametrize("secret", ["maker-secret-token", "auditor-secret-token", "inbound-secret-token",
                                    "sk-or-secret-key", "sk-ant-secret-key"])
def test_tokens_and_model_keys_are_redacted(root_handler, secret):
    record = logging.LogRecord("agent_layer", logging.WARNING, "f", 1, "upstream said: %s", (f"bad {secret}",), None)
    assert root_handler.filter(record)
    line = root_handler.format(record)
    assert secret not in line and REDACTED in line
