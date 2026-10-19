import logging
import re

import base58
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from pydantic import BaseModel, ValidationError

from gateway.core import crypto
from gateway.core.config import Settings
from gateway.core.errors import DomainError, install_error_handlers
from gateway.core.logging import (
    REDACTED,
    DropHealthChecksFilter,
    RedactSecretsFilter,
    configure_logging,
)


def test_canonical_json_sorts_keys_and_strips_whitespace() -> None:
    assert crypto.canonical_json({"b": 1, "a": {"d": [1, 2], "c": "ü"}}) == (
        '{"a":{"c":"ü","d":[1,2]},"b":1}'
    )


class TestVerifyEd25519:
    def setup_method(self) -> None:
        self.key = SigningKey.generate()
        self.pubkey = base58.b58encode(bytes(self.key.verify_key)).decode()

    def sign(self, message: str) -> str:
        return base58.b58encode(self.key.sign(message.encode()).signature).decode()

    def test_valid_signature(self) -> None:
        assert crypto.verify_ed25519(self.pubkey, "hello", self.sign("hello"))

    def test_tampered_message_fails(self) -> None:
        assert not crypto.verify_ed25519(self.pubkey, "hello!", self.sign("hello"))

    def test_other_key_fails(self) -> None:
        other = base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()
        assert not crypto.verify_ed25519(other, "hello", self.sign("hello"))

    @pytest.mark.parametrize(
        ("pubkey", "signature"),
        [("not-base58-0OIl", "x"), ("abc", "abc"), (None, "0OIl"), (None, "")],
    )
    def test_malformed_input_is_false(self, pubkey: str | None, signature: str) -> None:
        assert not crypto.verify_ed25519(pubkey or self.pubkey, "hello", signature)


def test_fernet_round_trip() -> None:
    key = Fernet.generate_key().decode()
    token = crypto.encrypt(key, b"secret chat")
    assert token != b"secret chat"
    assert crypto.decrypt(key, token) == b"secret chat"


def test_settings_load_from_environment() -> None:
    settings = Settings()
    assert settings.agent_url == "http://agent.test:8100"
    assert "test-maker-token-0123456789" in settings.secret_values()


def test_settings_refuse_shared_maker_and_auditor_token() -> None:
    """A shared token would let the maker post its own Auditor verdicts."""
    with pytest.raises(ValidationError, match="must differ"):
        Settings(platform_auditor_token="test-maker-token-0123456789")


def test_settings_cap_jwt_ttl_at_12_hours() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_ttl_hours=13)


def test_log_filter_redacts_secrets() -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "key=%s", ("s3cret-value",), None)
    RedactSecretsFilter(["s3cret-value"]).filter(record)
    assert record.getMessage() == f"key={REDACTED}"


def _access_record(path: str) -> logging.LogRecord:
    args = ("172.18.0.1:50000", "GET", path, "1.1", 200)
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "f", 1, '%s - "%s %s HTTP/%s" %d', args, None
    )


def test_log_filter_drops_only_health_checks() -> None:
    drop = DropHealthChecksFilter()
    assert drop.filter(_access_record("/healthz")) is False
    assert drop.filter(_access_record("/api/v1/requests")) is True
    other = logging.LogRecord(
        "gateway", logging.INFO, "f", 1, "%s %s %s", ("a", "b", "/healthz"), None
    )
    assert drop.filter(other) is True


def test_log_lines_carry_a_utc_timestamp() -> None:
    root = logging.getLogger()
    saved = root.handlers, root.level
    try:
        configure_logging([])
        line = root.handlers[0].format(_access_record("/api/v1/requests"))
    finally:
        root.handlers, root.level = saved
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO uvicorn\.access ", line)
    assert line.endswith('"GET /api/v1/requests HTTP/1.1" 200')


class _Body(BaseModel):
    amount_units: int


class _TeapotError(DomainError):
    code = "TEAPOT"
    status = 418


def _error_app() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/body")
    async def body(payload: _Body) -> dict[str, int]:
        return {"amount_units": payload.amount_units}

    @app.get("/domain")
    async def domain() -> None:
        raise _TeapotError("short and stout")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals at /srv/app.py line 42")

    return TestClient(app, raise_server_exceptions=False)


def test_domain_error_uses_envelope() -> None:
    response = _error_app().get("/domain")
    assert response.status_code == 418
    assert response.json() == {"error": {"code": "TEAPOT", "message": "short and stout"}}


def test_validation_error_does_not_echo_input() -> None:
    response = _error_app().post("/body", json={"amount_units": "DROP TABLE users"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "DROP TABLE" not in response.text


def test_unexpected_error_never_leaks_stack_trace() -> None:
    """SEC-19."""
    response = _error_app().get("/boom")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "Internal server error."}
    }
    assert "Traceback" not in response.text and "secret internals" not in response.text


def test_unknown_route_uses_envelope() -> None:
    response = _error_app().get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
