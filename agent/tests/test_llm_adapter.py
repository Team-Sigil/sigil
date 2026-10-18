"""AnthropicLLM builds valid SDK requests and maps responses and errors (no network)."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from agent_layer.contracts import AuditorVerdict
from agent_layer.llm import FALLBACK_BETA, AnthropicLLM, LLMUnavailable


def _message(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


def _llm(handler) -> tuple[AnthropicLLM, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(record)))
    return AnthropicLLM(client=client, effort="high"), seen


def test_create_sends_expected_request_and_maps_tool_use():
    llm, seen = _llm(lambda r: httpx2.Response(200, json=_message(
        [{"type": "tool_use", "id": "toolu_1", "name": "finish", "input": {"outcome": "declined"}}], "tool_use")))
    resp = llm.create(system="SYS", messages=[{"role": "user", "content": "hi"}],
                      tools=[{"name": "finish", "description": "d", "input_schema": {"type": "object"}}])

    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert FALLBACK_BETA in seen[0].headers["anthropic-beta"]
    assert resp.stop_reason == "tool_use"
    assert resp.content[0].name == "finish" and resp.content[0].input == {"outcome": "declined"}
    assert (resp.input_tokens, resp.output_tokens) == (120, 30)


def test_parse_returns_validated_object():
    payload = json.dumps({"verdict": "flag", "reason": "Receipt is for a game.", "concerns": []})
    llm, seen = _llm(lambda r: httpx2.Response(200, json=_message([{"type": "text", "text": payload}])))
    out = llm.parse(system="SYS", messages=[{"role": "user", "content": "x"}], output_format=AuditorVerdict)
    assert out.parsed == AuditorVerdict(verdict="flag", reason="Receipt is for a game.")
    assert json.loads(seen[0].content)["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("status", [400, 429, 500, 529])
def test_api_errors_become_llm_unavailable(status):
    llm, _ = _llm(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}}))
    with pytest.raises(LLMUnavailable):
        llm.create(system="S", messages=[{"role": "user", "content": "x"}], tools=[])


def test_connection_error_becomes_llm_unavailable():
    def boom(request):
        raise httpx2.ConnectError("down")

    llm, _ = _llm(boom)
    with pytest.raises(LLMUnavailable):
        llm.create(system="S", messages=[{"role": "user", "content": "x"}], tools=[])


def test_unparseable_structured_output_becomes_llm_unavailable():
    llm, _ = _llm(lambda r: httpx2.Response(200, json=_message([{"type": "text", "text": '{"verdict": "maybe"}'}])))
    with pytest.raises(LLMUnavailable):
        llm.parse(system="S", messages=[{"role": "user", "content": "x"}], output_format=AuditorVerdict)
