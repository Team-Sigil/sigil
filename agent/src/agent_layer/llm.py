"""Thin port over the Claude API so the agents can be tested with a scripted model.

``AnthropicLLM`` is the production implementation. Tests use
``tests.fakes.ScriptedLLM``, which returns pre-written responses with the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# Server-side refusal fallback: on a safety decline the API re-runs the request on a
# recommended fallback model instead of returning the refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    """The model could not be reached or returned an unusable response."""


@dataclass
class LLMResponse:
    content: list[Any]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


@dataclass
class ParsedResponse[P: BaseModel]:
    parsed: P | None
    response: LLMResponse


class LLM(Protocol):
    model: str

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...

    def parse(self, *, system: str, messages: list[dict], output_format: type[T]) -> ParsedResponse[T]: ...


class AnthropicLLM:
    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        effort: str = "high",
        max_tokens: int = 16_000,
        client: anthropic.Anthropic | None = None,
    ):
        self.model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._client = client or anthropic.Anthropic()

    def _common(self, system: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self._max_tokens,
            # The system prompt is static per agent, so it caches across requests.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }

    @staticmethod
    def _wrap(resp: Any) -> LLMResponse:
        return LLMResponse(
            content=list(resp.content),
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        try:
            resp = self._client.beta.messages.create(messages=messages, tools=tools, **self._common(system))
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailable(str(exc)) from exc
        return self._wrap(resp)

    def parse(self, *, system: str, messages: list[dict], output_format: type[T]) -> ParsedResponse[T]:
        try:
            resp = self._client.beta.messages.parse(
                messages=messages, output_format=output_format, **self._common(system)
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailable(str(exc)) from exc
        except ValueError as exc:  # schema validation of the parsed output failed
            raise LLMUnavailable(f"unparseable structured output: {exc}") from exc
        return ParsedResponse(parsed=resp.parsed_output, response=self._wrap(resp))
