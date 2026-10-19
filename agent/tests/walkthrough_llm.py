"""A deterministic, rule-based stand-in for Claude used by the walkthrough tests.

It plays both agents with simple, plausible rules so the outcomes documented in
walkthrough.md are reproducible without an API key:

* Maker: asks for proof at 25.00 or more when none is attached, otherwise requests
  the full amount asked for, guessing the category from the dependent's words. It is
  naive on purpose, so the guardrails and the Auditor have something to catch: it
  ignores what was already sent and takes "study materials" at face value. After a
  tool result it finishes with the matching outcome.
* Auditor: concerned (then flag) when the case mentions gems or a game store,
  otherwise supportive (then agree).
"""

from __future__ import annotations

import re

from agent_layer.contracts import (
    AuditorVerdict,
    Category,
    IndependentAssessment,
    PolicyDraft,
    TestScenario,
)
from agent_layer.flows.scheduled import SummaryText
from agent_layer.llm import ParsedResponse
from tests.fakes import all_text, finish, reply, text, tool_use

_AMOUNT = re.compile(r"Amount requested: (\d+\.\d{2})")
_MESSAGE = re.compile(r"<untrusted_dependent_message>\n(.*?)\n</untrusted_dependent_message>", re.S)
_SUSPICIOUS = ("gems", "gamestore", "game store")

DRAFT = PolicyDraft(
    weekly_cap=100, max_per_request=40, blocked_categories=[Category.GAMING], proof_threshold=25,
    allowance_amount=30, allowance_weekday=0,
    guidance="School supplies and bus fare are always fine. Ask first about trips.",
    test_scenarios=[TestScenario(scenario="12.00 for a novel for English class", what_i_would_do="Send it.")],
)


def _category(words: str) -> str:
    words = words.lower()
    if any(k in words for k in ("notebook", "calculator", "study", "book")):
        return "education"
    if "snack" in words or "lunch" in words:
        return "food"
    if "bus" in words:
        return "transport"
    return "other"


class WalkthroughLLM:
    def __init__(self, model: str = "walkthrough-model"):
        self.model = model
        self.supports_images = True
        self.supports_documents = True

    def create(self, *, system: str, messages: list[dict], tools: list[dict], timeout: float | None = None):
        last = messages[-1]["content"]
        if isinstance(last, list) and last and isinstance(last[0], dict) and last[0].get("type") == "tool_result":
            result = last[0]
            content = str(result["content"])
            if result.get("is_error"):
                return reply(finish("declined", "I can't send that right now, sorry.", "Declined: limits."))
            if content.startswith("Sent"):
                return reply(finish("sent_full", "Sent! Enjoy.", "Sent the requested amount."))
            if content.startswith("Proof requested"):
                return reply(finish("asked_proof", "Could you send a receipt or a photo of the price?",
                                    "Asked for proof."))
            return reply(finish("asked_guardian", "I've asked your guardian to decide.", "Guardian asked."))

        first = all_text(messages[:1])
        asked = float(m.group(1)) if (m := _AMOUNT.search(first)) else 0.0
        words = " ".join(_MESSAGE.findall(first))
        amount = asked
        if amount <= 0:
            return reply(finish("declined", "How much do you need?", "No amount given."))
        if amount >= 25 and "Proof:" not in first:
            return reply(tool_use("ask_for_proof", kind="receipt", reason="Larger amounts need a receipt."))
        return reply(
            text("This fits the guidance."),
            tool_use("request_transfer", amount=amount, purpose=words[:100] or "request",
                     category=_category(words), category_confidence=0.9,
                     reasoning="The request matches the guardian's guidance and the budget allows it.",
                     **({"proof_findings": {"merchant": "receipt", "date": "2026-10-13", "total": amount,
                                            "matches_claim": True}} if amount >= 25 else {})),
        )

    def parse(self, *, system: str, messages: list[dict], output_format: type, timeout: float | None = None):
        seen = all_text(messages).lower()
        concerned = any(k in seen for k in _SUSPICIOUS)
        if output_format is IndependentAssessment:
            parsed = IndependentAssessment(
                stance="concerned" if concerned else "supports",
                notes="The proof is from a game store." if concerned else "Consistent with the guidance.",
            )
        elif output_format is AuditorVerdict:
            parsed = (
                AuditorVerdict(verdict="flag", reason="The receipt is for in-game gems, not study materials.",
                               concerns=["category mismatch"])
                if concerned
                else AuditorVerdict(verdict="agree", reason="Amount and proof match the guidance.")
            )
        elif output_format is PolicyDraft:
            parsed = DRAFT
        elif output_format is SummaryText:
            parsed = SummaryText(summary="Mali spent on school supplies this week; one request went to you.")
        else:
            raise AssertionError(f"unexpected output format {output_format}")
        return ParsedResponse(parsed=parsed, response=reply(text("{}")))
