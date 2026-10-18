"""A scripted stand-in for the Claude API with the same response shape as AnthropicLLM."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from agent_layer.llm import LLMResponse, LLMUnavailable, ParsedResponse

_ids = itertools.count(1)


def tool_use(name: str, **input: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=input)


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def reply(*blocks: SimpleNamespace, stop_reason: str | None = None, tokens: int = 100) -> LLMResponse:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return LLMResponse(content=list(blocks), stop_reason=stop_reason, input_tokens=tokens, output_tokens=tokens // 2)


def finish(outcome: str, reply_text: str = "Okay!", summary: str = "Handled the request.") -> SimpleNamespace:
    return tool_use("finish", outcome=outcome, reply_to_dependent=reply_text, summary_for_guardian=summary)


def findings(total: float, merchant: str = "Office Mart", *, matches: bool = True,
             receipt_number: str | None = None, date: str = "2026-10-13") -> dict:
    return {"merchant": merchant, "date": date, "total": total, "receipt_number": receipt_number,
            "matches_claim": matches}


def transfer(amount: float, category: str = "education", *, confidence: float = 0.95,
             purpose: str = "school supplies", reasoning: str = "Fits the guidance for school costs.",
             proof: dict | None = None) -> SimpleNamespace:
    args = dict(amount=amount, purpose=purpose, category=category, category_confidence=confidence,
                reasoning=reasoning)
    if proof is not None:
        args["proof_findings"] = proof
    return tool_use("request_transfer", **args)


Step = LLMResponse | BaseModel | Exception | Callable[[list[dict]], Any]


class ScriptedLLM:
    """Returns scripted steps in order. ``create`` steps are LLMResponse; ``parse`` steps
    are the parsed Pydantic object (or None). An Exception step is raised. A callable
    step receives the messages and returns one of the above."""

    def __init__(self, steps: list[Step] | None = None, model: str = "scripted-model"):
        self.model = model
        self.steps = list(steps or [])
        self.calls: list[dict[str, Any]] = []

    def _next(self, messages: list[dict]) -> Any:
        if not self.steps:
            raise AssertionError("ScriptedLLM ran out of steps")
        step = self.steps.pop(0)
        if callable(step) and not isinstance(step, (BaseModel, Exception)):
            step = step(messages)
        if isinstance(step, Exception):
            raise step
        return step

    def create(self, *, system: str, messages: list[dict], tools: list[dict], timeout: float | None = None) -> LLMResponse:
        self.calls.append({"kind": "create", "system": system, "messages": copy.deepcopy(messages), "tools": tools,
                           "timeout": timeout})
        step = self._next(messages)
        assert isinstance(step, LLMResponse), f"create() got {type(step)}"
        return step

    def parse(self, *, system: str, messages: list[dict], output_format: type, timeout: float | None = None) -> ParsedResponse:
        self.calls.append({"kind": "parse", "system": system, "messages": copy.deepcopy(messages),
                           "output_format": output_format, "timeout": timeout})
        step = self._next(messages)
        if isinstance(step, LLMResponse):  # e.g. a refusal
            return ParsedResponse(parsed=None, response=step)
        if step is not None and not isinstance(step, output_format):
            raise AssertionError(f"parse() expected {output_format.__name__}, got {type(step).__name__}")
        return ParsedResponse(parsed=step, response=reply(text("{}")))

    @property
    def remaining(self) -> int:
        return len(self.steps)


def unavailable() -> LLMUnavailable:
    return LLMUnavailable("simulated outage")


def all_text(messages: list[dict]) -> str:
    """Flatten every text the model was shown, for assertions."""
    out: list[str] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    out.append(block["text"])
                elif block.get("type") == "tool_result":
                    out.append(str(block.get("content")))
            elif getattr(block, "type", None) == "text":
                out.append(block.text)
    return "\n".join(out)
