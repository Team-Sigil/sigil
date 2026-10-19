"""OpenRouterLLM translates the agents' Anthropic-style messages to OpenAI-style chat and
back, and the provider is switchable per agent (no network: a mock transport)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from agent_layer.config import Settings
from agent_layer.llm import AnthropicLLM, LLMUnavailable
from agent_layer.llm_openrouter import OpenRouterLLM
from agent_layer.maker.tools import tool_definitions

MODEL = "nvidia/nemotron-3-super-120b-a12b"


def _completion(message: dict, finish: str = "stop", usage=(100, 20)) -> dict:
    return {"id": "gen-1", "choices": [{"message": {"role": "assistant", **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def _llm(*responses, vision: bool = False, effort: str = "high"):
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = queue.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    llm = OpenRouterLLM(MODEL, api_key="sk-or-test", effort=effort, vision=vision,
                        transport=httpx.MockTransport(handler))
    return llm, seen


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# ---------------------------------------------------------------- requests


def test_create_sends_an_openai_style_request():
    llm, seen = _llm(_completion({"content": "ok"}))
    llm.create(system="SYS", messages=[{"role": "user", "content": "hi"}], tools=tool_definitions("request"))
    req = seen[0]
    body = _body(req)
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == "Bearer sk-or-test"
    assert body["model"] == MODEL and body["max_tokens"] == 16_000
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["reasoning"] == {"effort": "high", "exclude": True}
    assert body["provider"] == {"require_parameters": True}  # never routed to a provider without tools
    names = {t["function"]["name"] for t in body["tools"]}
    assert names == {"request_transfer", "ask_for_proof", "ask_guardian", "propose_policy_update",
                     "notify_guardian", "finish"}
    finish = next(t for t in body["tools"] if t["function"]["name"] == "finish")
    assert finish["type"] == "function" and "outcome" in finish["function"]["parameters"]["properties"]


@pytest.mark.parametrize("effort,sent", [("low", "low"), ("medium", "medium"), ("xhigh", "high"), ("max", "high")])
def test_effort_maps_to_openrouter_reasoning(effort, sent):
    llm, seen = _llm(_completion({"content": "ok"}), effort=effort)
    llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert _body(seen[0])["reasoning"]["effort"] == sent
    assert "tools" not in _body(seen[0])


def test_tool_history_round_trips():
    """The agent loop's own history: an assistant turn with a tool call (as this adapter
    returned it), then the tool result in a user turn, then a nudge."""
    llm, seen = _llm(_completion({"content": "done"}))
    assistant = [SimpleNamespace(type="text", text="Let me check."),
                 SimpleNamespace(type="tool_use", id="call_1", name="finish", input={"outcome": "declined"})]
    llm.create(system="S", tools=[], messages=[
        {"role": "user", "content": [{"type": "text", "text": "request"}]},
        {"role": "assistant", "content": assistant},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Rejected.",
                                      "is_error": True}]},
        {"role": "user", "content": "Please call finish."},
    ])
    msgs = _body(seen[0])["messages"]
    assert msgs[1] == {"role": "user", "content": [{"type": "text", "text": "request"}]}
    assert msgs[2]["role"] == "assistant" and msgs[2]["content"] == "Let me check."
    assert msgs[2]["tool_calls"] == [{"id": "call_1", "type": "function",
                                      "function": {"name": "finish", "arguments": '{"outcome": "declined"}'}}]
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "Error: Rejected."}
    assert msgs[4] == {"role": "user", "content": "Please call finish."}


def test_images_are_sent_only_to_a_vision_model():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(b"png").decode()}}
    turn = [{"role": "user", "content": [image, {"type": "text", "text": "receipt"}]}]
    llm, seen = _llm(_completion({"content": "ok"}), vision=True)
    llm.create(system="S", messages=turn, tools=[])
    part = _body(seen[0])["messages"][1]["content"][0]
    assert part == {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}

    text_only, _ = _llm(_completion({"content": "ok"}))
    assert text_only.supports_images is False
    with pytest.raises(LLMUnavailable, match="without image input"):  # refused, never silently dropped
        text_only.create(system="S", messages=turn, tools=[])


# --------------------------------------------------------------- responses


def test_tool_calls_become_tool_use_blocks():
    llm, _ = _llm(_completion({"content": None, "tool_calls": [
        {"id": "call_9", "type": "function", "function": {"name": "request_transfer",
                                                          "arguments": '{"amount": 10, "category": "food"}'}}]},
        finish="tool_calls", usage=(321, 45)))
    resp = llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[])
    (block,) = resp.content
    assert (block.type, block.id, block.name, block.input) == ("tool_use", "call_9", "request_transfer",
                                                               {"amount": 10, "category": "food"})
    assert resp.stop_reason == "tool_use" and (resp.input_tokens, resp.output_tokens) == (321, 45)


def test_unparseable_tool_arguments_reach_the_guard_visibly():
    llm, _ = _llm(_completion({"tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "finish", "arguments": "{not json"}}]}, "tool_calls"))
    (block,) = llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[]).content
    assert block.input == {"_unparseable_arguments": "{not json"}  # extra field: the guard rejects it


@pytest.mark.parametrize("finish,stop", [("stop", "end_turn"), ("length", "max_tokens"),
                                         ("content_filter", "refusal")])
def test_stop_reasons(finish, stop):
    llm, _ = _llm(_completion({"content": "x"}, finish))
    assert llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[]).stop_reason == stop


def test_retries_once_then_gives_up():
    busy = httpx.Response(503, json={"error": "busy"})
    llm, seen = _llm(busy, _completion({"content": "ok"}))
    assert llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[]).content[0].text == "ok"
    assert len(seen) == 2
    llm, _ = _llm(busy, busy)
    with pytest.raises(LLMUnavailable, match="503"):
        llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[])


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": {"message": "bad request"}}),
    httpx.Response(200, json={"error": {"message": "no provider supports the parameters"}}),
    httpx.Response(200, content=b"not json"),
])
def test_unusable_answers_raise_llm_unavailable(response):
    llm, _ = _llm(response)
    with pytest.raises(LLMUnavailable):
        llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[])


def test_the_deadline_is_split_over_the_retry():
    llm, seen = _llm(_completion({"content": "ok"}))
    llm.create(system="S", messages=[{"role": "user", "content": "hi"}], tools=[], timeout=40)
    assert seen[0].extensions["timeout"]["read"] == 20


# -------------------------------------------------------- structured output


class Verdict(BaseModel):
    verdict: str
    reason: str


def test_parse_asks_for_the_schema_and_validates():
    llm, seen = _llm(_completion({"content": '{"verdict": "agree", "reason": "fine"}'}))
    out = llm.parse(system="AUDIT", messages=[{"role": "user", "content": "case"}], output_format=Verdict)
    assert out.parsed == Verdict(verdict="agree", reason="fine")
    body = _body(seen[0])
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == Verdict.model_json_schema()
    assert body["messages"][0]["content"].startswith("AUDIT") and '"verdict"' in body["messages"][0]["content"]


def test_parse_accepts_a_fenced_answer():
    llm, _ = _llm(_completion({"content": '```json\n{"verdict": "flag", "reason": "odd"}\n```'}))
    assert llm.parse(system="S", messages=[{"role": "user", "content": "c"}], output_format=Verdict).parsed.verdict \
        == "flag"


def test_parse_repairs_once_and_counts_both_attempts():
    llm, seen = _llm(_completion({"content": '{"verdict": "agree"}'}, usage=(10, 5)),
                     _completion({"content": '{"verdict": "agree", "reason": "fixed"}'}, usage=(12, 6)))
    out = llm.parse(system="S", messages=[{"role": "user", "content": "c"}], output_format=Verdict)
    assert out.parsed.reason == "fixed"
    assert (out.response.input_tokens, out.response.output_tokens) == (22, 11)  # both count toward the budget
    repair = _body(seen[1])["messages"][-1]["content"]
    assert "reason: Field required" in repair


def test_parse_gives_up_after_the_repair():
    bad = _completion({"content": "I think it's fine."})
    llm, _ = _llm(bad, bad)
    with pytest.raises(LLMUnavailable, match="unparseable"):
        llm.parse(system="S", messages=[{"role": "user", "content": "c"}], output_format=Verdict)


def test_parse_reports_a_refusal():
    llm, _ = _llm(_completion({"content": "No."}, "content_filter"))
    out = llm.parse(system="S", messages=[{"role": "user", "content": "c"}], output_format=Verdict)
    assert out.parsed is None and out.response.stop_reason == "refusal"


# ---------------------------------------------------------------- switching


def _env(monkeypatch, **values):
    base = {"PLATFORM_API_URL": "http://platform", "PLATFORM_MAKER_TOKEN": "m", "PLATFORM_AUDITOR_TOKEN": "a"}
    for k in ("AGENT_LLM_PROVIDER", "AGENT_MAKER_PROVIDER", "AGENT_AUDITOR_PROVIDER", "AGENT_MAKER_MODEL",
              "AGENT_AUDITOR_MODEL", "OPENROUTER_API_KEY", "AGENT_OPENROUTER_VISION",
              "AGENT_OPENROUTER_DOCUMENTS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**base, **values}.items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def test_default_is_claude(monkeypatch):
    s = _env(monkeypatch)
    assert (s.maker_provider, s.auditor_provider, s.maker_model) == ("anthropic", "anthropic", "claude-opus-5-5")


def test_openrouter_for_both_with_the_nemotron_default(monkeypatch):
    s = _env(monkeypatch, AGENT_LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or")
    assert (s.maker_provider, s.auditor_provider) == ("openrouter", "openrouter")
    assert s.maker_model == s.auditor_model == MODEL and s.openrouter_vision is False


def test_per_agent_override_and_model(monkeypatch):
    s = _env(monkeypatch, AGENT_LLM_PROVIDER="openrouter", AGENT_AUDITOR_PROVIDER="anthropic",
             OPENROUTER_API_KEY="sk-or", AGENT_MAKER_MODEL="some/other-model")
    assert (s.maker_provider, s.maker_model) == ("openrouter", "some/other-model")
    assert (s.auditor_provider, s.auditor_model) == ("anthropic", "claude-opus-5-5")


def test_openrouter_needs_a_key_and_a_known_provider(monkeypatch):
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _env(monkeypatch, AGENT_LLM_PROVIDER="openrouter")
    with pytest.raises(ValueError, match="AGENT_LLM_PROVIDER"):
        _env(monkeypatch, AGENT_LLM_PROVIDER="gpt")


def test_runtime_builds_each_agents_provider(monkeypatch):
    from agent_layer.service import build_runtime

    s = _env(monkeypatch, AGENT_LLM_PROVIDER="openrouter", AGENT_AUDITOR_PROVIDER="anthropic",
             OPENROUTER_API_KEY="sk-or", ANTHROPIC_API_KEY="sk-ant-test", AGENT_OPENROUTER_VISION="1")
    runtime = build_runtime(s)
    assert isinstance(runtime.maker_llm, OpenRouterLLM) and runtime.maker_llm.supports_images is True
    assert isinstance(runtime.maker._auditor._llm, AnthropicLLM)


def test_sandbox_allows_only_the_providers_in_use(monkeypatch):
    from agent_layer.__main__ import allowed_hosts

    s = _env(monkeypatch, AGENT_LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or")
    assert allowed_hosts(s) == ["platform", "openrouter.ai"]
    assert allowed_hosts(_env(monkeypatch)) == ["platform", "api.anthropic.com"]


def test_pdfs_are_sent_only_to_a_model_that_reads_documents():
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERg=="}}
    turn = [{"role": "user", "content": [doc, {"type": "text", "text": "receipt"}]}]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion({"content": "ok"}))

    llm = OpenRouterLLM(MODEL, api_key="k", documents=True, transport=httpx.MockTransport(handler))
    llm.create(system="S", messages=turn, tools=[])
    part = _body(seen[0])["messages"][1]["content"][0]
    assert part == {"type": "file", "file": {"filename": "receipt.pdf",
                                             "file_data": "data:application/pdf;base64,JVBERg=="}}

    text_only, _ = _llm(_completion({"content": "ok"}))
    assert text_only.supports_documents is False
    with pytest.raises(LLMUnavailable, match="without document input"):
        text_only.create(system="S", messages=turn, tools=[])


def test_document_input_is_a_setting(monkeypatch):
    s = _env(monkeypatch, AGENT_LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or", AGENT_OPENROUTER_DOCUMENTS="1")
    assert s.openrouter_documents is True
    from agent_layer.service import build_llm

    assert build_llm(s, "openrouter", MODEL, "high").supports_documents is True
    assert build_llm(s, "anthropic", "claude-opus-5-5", "high").supports_documents is True
