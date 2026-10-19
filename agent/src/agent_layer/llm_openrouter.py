"""The LLM port over OpenRouter (OpenAI-style chat completions), e.g. for NVIDIA
Nemotron 3 Super.

The agents speak Anthropic-style messages and content blocks (``llm.py``). This adapter
translates both ways, so nothing above it changes when the provider does:

* tool definitions → ``tools`` (functions); ``tool_use`` blocks ⇄ ``tool_calls``;
  ``tool_result`` blocks → ``tool`` messages;
* image blocks → ``image_url`` data URIs, only when the model can see images (otherwise
  the agents never send them; see ``supports_images``);
* ``parse`` asks for JSON matching the Pydantic schema (``response_format``), validates
  it, and gives the model one chance to repair invalid output;
* effort → OpenRouter's ``reasoning.effort``; the reasoning text itself is not returned.

Requests are routed only to OpenRouter providers that support every parameter sent
(``provider.require_parameters``), so tools or structured output are never silently
dropped by a provider that lacks them.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import httpx
from pydantic import ValidationError

from agent_layer.llm import LLMResponse, LLMUnavailable, ParsedResponse, T

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0
_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
_STOP_REASONS = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read a content block that may be a dict, an SDK object or a SimpleNamespace."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(_get(b, "text", "") for b in content or [] if _get(b, "type") == "text")


class OpenRouterLLM:
    def __init__(
        self,
        model: str = "nvidia/nemotron-3-super-120b-a12b",
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        effort: str = "high",
        max_tokens: int = 16_000,
        vision: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.supports_images = vision
        self._effort = _EFFORTS.get(effort, "high")
        self._max_tokens = max_tokens
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Sigil agent layer"},
            transport=transport,
            follow_redirects=False,
        )

    # -------------------------------------------------------------- translation

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            role, content = m["role"], m["content"]
            if role == "assistant":
                out.append(self._assistant(content))
            elif isinstance(content, str):
                out.append({"role": role, "content": content})
            else:
                out.extend(self._user_blocks(content))
        return out

    @staticmethod
    def _assistant(content: Any) -> dict:
        if isinstance(content, str):
            return {"role": "assistant", "content": content}
        msg: dict[str, Any] = {"role": "assistant", "content": _text_of(content) or None}
        calls = [
            {"id": _get(b, "id"), "type": "function",
             "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}}
            for b in content if _get(b, "type") == "tool_use"
        ]
        if calls:
            msg["tool_calls"] = calls
        return msg

    def _user_blocks(self, blocks: list[Any]) -> list[dict]:
        """Tool results become ``tool`` messages (in order, before anything else the user
        turn holds); text and images become one user message."""
        tool_msgs: list[dict] = []
        parts: list[dict] = []
        for b in blocks:
            kind = _get(b, "type")
            if kind == "tool_result":
                text = _text_of(_get(b, "content", ""))
                if _get(b, "is_error"):
                    text = f"Error: {text}"
                tool_msgs.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"), "content": text})
            elif kind == "text":
                parts.append({"type": "text", "text": _get(b, "text", "")})
            elif kind == "image":
                if not self.supports_images:
                    # The agents never send images to a text-only model; refuse rather than drop.
                    raise LLMUnavailable("image sent to a model without image input")
                src = _get(b, "source", {})
                url = f"data:{_get(src, 'media_type')};base64,{_get(src, 'data')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        return tool_msgs + ([{"role": "user", "content": parts}] if parts else [])

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        return [
            {"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
            for t in tools
        ]

    @staticmethod
    def _response(data: dict) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable(f"no choices in response: {str(data.get('error'))[:200]}")
        choice = choices[0]
        msg = choice.get("message") or {}
        blocks: list[Any] = []
        if msg.get("content"):
            blocks.append(SimpleNamespace(type="text", text=msg["content"]))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments are not an object")
            except ValueError:
                # Kept visible so the tool guard rejects it with a reason the model can act on.
                args = {"_unparseable_arguments": str(fn.get("arguments"))[:200]}
            blocks.append(SimpleNamespace(type="tool_use", id=call.get("id"), name=fn.get("name"), input=args))
        reason = choice.get("finish_reason")
        stop = "tool_use" if msg.get("tool_calls") else _STOP_REASONS.get(reason, reason)
        if reason == "error":
            raise LLMUnavailable("provider error during generation")
        usage = data.get("usage") or {}
        return LLMResponse(
            content=blocks, stop_reason=stop,
            input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0),
        )

    # -------------------------------------------------------------------- calls

    def _post(self, body: dict, timeout: float | None) -> dict:
        """One retry on a transport error or 429/5xx; half the time left per attempt when a
        deadline applies, so the retry can't overrun it."""
        per_try = (timeout / 2) if timeout else DEFAULT_TIMEOUT
        last = "no attempt"
        for _ in range(2):
            try:
                resp = self._http.post("/chat/completions", json=body, timeout=per_try)
            except httpx.HTTPError as exc:
                last = f"transport failure: {type(exc).__name__}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"OpenRouter answered {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise LLMUnavailable(f"OpenRouter answered {resp.status_code}: {resp.text[:200]}")
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise LLMUnavailable("OpenRouter response too large")
            try:
                return resp.json()
            except ValueError as exc:
                raise LLMUnavailable("OpenRouter sent invalid JSON") from exc
        raise LLMUnavailable(last)

    def _body(self, system: str, messages: list[dict], **extra: Any) -> dict:
        return {
            "model": self.model,
            "messages": self._messages(system, messages),
            "max_tokens": self._max_tokens,
            "reasoning": {"effort": self._effort, "exclude": True},
            "provider": {"require_parameters": True},
            **extra,
        }

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict], timeout: float | None = None
    ) -> LLMResponse:
        extra: dict[str, Any] = {"tools": self._tools(tools), "tool_choice": "auto"} if tools else {}
        return self._response(self._post(self._body(system, messages, **extra), timeout))

    def parse(
        self, *, system: str, messages: list[dict], output_format: type[T], timeout: float | None = None
    ) -> ParsedResponse[T]:
        schema = output_format.model_json_schema()
        fmt = {"type": "json_schema",
               "json_schema": {"name": output_format.__name__, "schema": schema, "strict": False}}
        instruction = (
            f"{system}\n\nAnswer with one JSON object only, no other text, matching this JSON schema:\n"
            f"{json.dumps(schema)}"
        )
        convo = list(messages)
        total_in = total_out = 0
        for attempt in range(2):
            resp = self._response(self._post(self._body(instruction, convo, response_format=fmt), timeout))
            total_in, total_out = total_in + resp.input_tokens, total_out + resp.output_tokens
            if resp.stop_reason == "refusal":
                return ParsedResponse(parsed=None, response=_with_usage(resp, total_in, total_out))
            raw = _text_of(resp.content).strip()
            fenced = _FENCE.match(raw)
            try:
                parsed = output_format.model_validate_json(fenced.group(1) if fenced else raw)
                return ParsedResponse(parsed=parsed, response=_with_usage(resp, total_in, total_out))
            except ValidationError as exc:
                if attempt == 1:
                    raise LLMUnavailable(f"unparseable structured output: {exc.error_count()} errors") from exc
                convo += [
                    {"role": "assistant", "content": raw[:4_000] or "(empty)"},
                    {"role": "user", "content": "That wasn't valid for the schema: "
                     + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10])
                     + ". Answer again with the corrected JSON object only."},
                ]
        raise AssertionError("unreachable")


def _with_usage(resp: LLMResponse, input_tokens: int, output_tokens: int) -> LLMResponse:
    """The tokens of every attempt count toward the family's budget."""
    return LLMResponse(content=resp.content, stop_reason=resp.stop_reason,
                       input_tokens=input_tokens, output_tokens=output_tokens)

