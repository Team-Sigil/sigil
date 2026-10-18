"""The Auditor agent (checker).

It reviews a proposed transfer in two phases inside one conversation: an independent
assessment of the raw case first, then a verdict on the maker's decision. It is
monotonic (can only make an outcome stricter) and fails closed (any error is a flag).
Its verdict is posted with the Auditor's own credential and bound to one exact
transfer (idempotency key + amount + category).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_layer.auditor.prompts import AUDITOR_SYSTEM, PHASE1_INSTRUCTION, PHASE2_INSTRUCTION
from agent_layer.contracts import (
    AgentContext,
    AuditorVerdict,
    Category,
    IndependentAssessment,
    VerdictSubmission,
)
from agent_layer.guardrails.budget import BudgetExhausted, TokenBudget
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import wrap_untrusted
from agent_layer.llm import LLM, LLMUnavailable
from agent_layer.money import to_display
from agent_layer.platform_api import AuditorAPI, PlatformAPIError


@dataclass(frozen=True)
class ReviewCase:
    ctx: AgentContext
    request_id: str | None
    case_text: str  # already sanitised: the dependent's words, or the scheduled trigger
    proof_texts: list[str]
    proof_images: list[dict]  # image content blocks, same as the maker saw
    amount: int
    category: Category
    purpose: str
    maker_reasoning: str
    category_confidence: float
    idempotency_key: str
    triggers: list[str]


@dataclass
class ReviewResult:
    verdict: AuditorVerdict
    verdict_id: str | None
    assessment: IndependentAssessment | None
    events: list[str] = field(default_factory=list)

    @property
    def agreed(self) -> bool:
        return self.verdict.verdict == "agree" and self.verdict_id is not None


def _flag(reason: str) -> AuditorVerdict:
    return AuditorVerdict(verdict="flag", reason=reason, concerns=[])


class Auditor:
    def __init__(self, llm: LLM, api: AuditorAPI):
        self._llm = llm
        self._api = api

    def _case_message(self, case: ReviewCase) -> str:
        ctx, p = case.ctx, case.ctx.policy
        history = "\n".join(
            f"- {h.at:%a %d %b}: {h.kind} {to_display(h.amount)} ({h.category}) {h.purpose}"
            for h in ctx.history[-15:]
        ) or "- (none)"
        proofs = "\n".join(
            wrap_untrusted("untrusted_proof", t, index=str(i)) for i, t in enumerate(case.proof_texts)
        ) or ("(see attached images)" if case.proof_images else "(no proof provided)")
        return (
            f"Dependent: {ctx.dependent_name}\n"
            f"Why you are reviewing: {', '.join(case.triggers)}\n\n"
            f"<guardian_guidance>\n{p.guidance or '(none given)'}\n</guardian_guidance>\n\n"
            f"Weekly cap {to_display(p.weekly_cap)}, spent this week "
            f"{to_display(ctx.budget.spent_this_week)}, left {to_display(ctx.budget.remaining_this_week)}.\n"
            f"Recent history:\n{history}\n\n"
            f"The request:\n{case.case_text}\n\nProof:\n{proofs}\n\n{PHASE1_INSTRUCTION}"
        )

    def _decision_message(self, case: ReviewCase) -> str:
        return (
            "The maker decided to send "
            f"{to_display(case.amount)} as category '{case.category}' "
            f"(confidence {case.category_confidence:.2f}) for: {case.purpose}\n"
            f"<maker_reasoning>\n{case.maker_reasoning}\n</maker_reasoning>\n\n{PHASE2_INSTRUCTION}"
        )

    def review(self, case: ReviewCase, budget: TokenBudget, log: GuardrailLog) -> ReviewResult:
        assessment: IndependentAssessment | None = None
        try:
            budget.ensure_available()
            content = [*case.proof_images, {"type": "text", "text": self._case_message(case)}]
            messages: list[dict] = [{"role": "user", "content": content}]
            first = self._llm.parse(system=AUDITOR_SYSTEM, messages=messages, output_format=IndependentAssessment)
            budget.record(first.response)
            if first.parsed is None or first.response.stop_reason == "refusal":
                raise LLMUnavailable("no independent assessment")
            assessment = first.parsed

            messages += [
                {"role": "assistant", "content": first.response.content},
                {"role": "user", "content": self._decision_message(case)},
            ]
            second = self._llm.parse(system=AUDITOR_SYSTEM, messages=messages, output_format=AuditorVerdict)
            budget.record(second.response)
            if second.parsed is None or second.response.stop_reason == "refusal":
                raise LLMUnavailable("no verdict")
            verdict = second.parsed
        except (LLMUnavailable, BudgetExhausted, PlatformAPIError) as exc:
            log.add("auditor_fail_closed", type(exc).__name__)
            verdict = _flag("The Auditor could not complete its review, so the guardian should decide.")

        # Persuasion guard: an independent "concerned" view that flips to "agree" after
        # reading the maker's reasoning is exactly the both-agents-fooled failure mode.
        if assessment is not None and assessment.stance == "concerned" and verdict.verdict == "agree":
            log.add("auditor_flip_blocked")
            verdict = _flag(
                "The Auditor's independent view was concerned before it read the agent's reasoning: "
                + assessment.notes
            )

        verdict_id: str | None = None
        try:
            receipt = self._api.submit_verdict(
                VerdictSubmission(
                    dependent_id=case.ctx.dependent_id,
                    request_id=case.request_id,
                    idempotency_key=case.idempotency_key,
                    amount=case.amount,
                    category=case.category,
                    verdict=verdict.verdict,
                    reason=verdict.reason,
                    concerns=verdict.concerns,
                    independent_assessment=assessment
                    or IndependentAssessment(stance="unsure", notes="not available"),
                    review_triggers=case.triggers,
                    model=self._llm.model,
                )
            )
            verdict_id = receipt.verdict_id
        except PlatformAPIError:
            log.add("auditor_verdict_not_recorded")
            if verdict.verdict == "agree":
                verdict = _flag("The Auditor's approval could not be recorded, so the guardian should decide.")
        return ReviewResult(verdict=verdict, verdict_id=verdict_id, assessment=assessment)
