"""The Auditor agent (checker).

It reviews a proposed transfer in two phases inside one conversation: an independent
assessment of the raw case first, then a verdict on the maker's decision.

It reads the dependent's context (policy, guidance, budget, history) itself, with its own
token, rather than trusting the maker's snapshot: a compromised maker can't feed it a
doctored policy or history. If the maker's policy differs from the platform's, or the
family is frozen, it flags without calling the model. It is
monotonic (can only make an outcome stricter) and fails closed (any error is a flag).
Its verdict is posted with the Auditor's own credential and bound to one exact
transfer (idempotency key + amount + category).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_layer.auditor.prompts import AUDITOR_SYSTEM, PHASE1_INSTRUCTION, PHASE2_INSTRUCTION
from agent_layer.config import Limits
from agent_layer.contracts import (
    AgentContext,
    AuditorVerdict,
    Category,
    IndependentAssessment,
    VerdictSubmission,
)
from agent_layer.guardrails.budget import BudgetExhausted, TokenBudget
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import PlatformText, wrap_untrusted
from agent_layer.i18n import LANGUAGE_NAMES, guardian_locale
from agent_layer.llm import LLM, LLMUnavailable
from agent_layer.maker.context import dependent_name, guidance_block, history_lines
from agent_layer.money import to_display
from agent_layer.platform_api import AuditorAPI, PlatformAPIError

logger = logging.getLogger(__name__)


class ContextConflict(Exception):
    """The maker's view of the policy doesn't match the platform's."""


def _policy_conflict(maker: AgentContext, own: AgentContext) -> str | None:
    if (own.dependent_id, own.guardian_id) != (maker.dependent_id, maker.guardian_id):
        return "identity"
    if own.frozen:
        return "frozen"
    if (own.policy.version, own.policy.policy_hash) != (maker.policy.version, maker.policy.policy_hash):
        return "policy_version"
    if own.policy != maker.policy:  # same version, different content: the snapshot was altered
        return "policy_content"
    return None


@dataclass(frozen=True)
class ReviewCase:
    ctx: AgentContext  # the maker's snapshot: only used to check against the Auditor's own read
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
    proof_documents: list[dict] = field(default_factory=list)  # signed PDF receipts, as document blocks


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
    def __init__(self, llm: LLM, api: AuditorAPI, limits: Limits | None = None):
        self._llm = llm
        self._api = api
        self._limits = limits or Limits()

    def _case_message(self, case: ReviewCase, ctx: AgentContext, pt: PlatformText) -> str:
        p, limits = ctx.policy, self._limits
        proofs = "\n".join(
            wrap_untrusted("untrusted_proof", t, index=str(i)) for i, t in enumerate(case.proof_texts)
        ) or ("(see attached images)" if case.proof_images else "(no proof provided)")
        return (
            f"Dependent: {dependent_name(ctx, limits, pt)}\n"
            f"Why you are reviewing: {', '.join(case.triggers)}\n\n"
            f"{guidance_block(ctx, limits, pt)}\n\n"
            f"Weekly cap {to_display(p.weekly_cap)}, spent this week "
            f"{to_display(ctx.budget.spent_this_week)}, left {to_display(ctx.budget.remaining_this_week)}.\n"
            f"Recent history:\n{history_lines(ctx, limits, pt)}\n\n"
            f"The request:\n{case.case_text}\n\nProof:\n{proofs}\n\n{PHASE1_INSTRUCTION}\n"
            f"Write your notes, reason and concerns in {LANGUAGE_NAMES[guardian_locale(ctx)]}: "
            "the guardian reads them."
        )

    def _decision_message(self, case: ReviewCase, pt: PlatformText) -> str:
        # The maker is untrusted too: its purpose and reasoning are escaped like input.
        purpose = pt.inline(case.purpose, max_chars=self._limits.max_purpose_chars, source="maker_purpose")
        return (
            "The maker decided to send "
            f"{to_display(case.amount)} as category '{case.category}' "
            f"(confidence {case.category_confidence:.2f}) for: {purpose}\n"
            + wrap_untrusted("maker_reasoning", case.maker_reasoning)
            + f"\n\n{PHASE2_INSTRUCTION}"
        )

    def review(
        self,
        case: ReviewCase,
        budget: TokenBudget,
        log: GuardrailLog,
        time_left: Callable[[], float] | None = None,
    ) -> ReviewResult:
        """``time_left`` gives each call's timeout and raises ``RunDeadline`` when the run's
        time is spent, which fails closed like any other error."""
        assessment: IndependentAssessment | None = None
        pt = PlatformText(log)
        try:
            own = self._api.get_context(case.ctx.dependent_id)
            if conflict := _policy_conflict(case.ctx, own):
                log.add("auditor_context_mismatch", conflict)
                raise ContextConflict(conflict)
            budget.ensure_available()
            images = case.proof_images if self._llm.supports_images else []
            documents = case.proof_documents if self._llm.supports_documents else []
            message = self._case_message(case, own, pt)
            if documents:
                message += (
                    f"\n\n{len(documents)} PDF receipt(s) are attached, signed by the shop: the platform verified "
                    "they came from the shop unchanged, not that the order is this dependent's."
                )
            for n, what, event in (
                (len(case.proof_images) - len(images), "image proof(s) were attached, but you can't see images",
                 "auditor_proof_unseen"),
                (len(case.proof_documents) - len(documents), "PDF receipt(s) were attached, but you can't read PDFs",
                 "auditor_document_unseen"),
            ):
                if n:
                    log.add(event, str(n))
                    message += (
                        f"\n\n{n} {what}. You can't confirm what they show: weigh that, and flag if the case "
                        "depends on them."
                    )
            content = [*images, *documents, {"type": "text", "text": message}]
            messages: list[dict] = [{"role": "user", "content": content}]
            first = self._llm.parse(
                system=AUDITOR_SYSTEM, messages=messages, output_format=IndependentAssessment,
                timeout=time_left() if time_left else None,
            )
            budget.record(first.response)
            if first.parsed is None or first.response.stop_reason == "refusal":
                raise LLMUnavailable("no independent assessment")
            assessment = first.parsed

            messages += [
                {"role": "assistant", "content": first.response.content},
                {"role": "user", "content": self._decision_message(case, pt)},
            ]
            second = self._llm.parse(
                system=AUDITOR_SYSTEM, messages=messages, output_format=AuditorVerdict,
                timeout=time_left() if time_left else None,
            )
            budget.record(second.response)
            if second.parsed is None or second.response.stop_reason == "refusal":
                raise LLMUnavailable("no verdict")
            verdict = second.parsed
        except ContextConflict as exc:
            verdict = _flag(
                "The family is frozen." if str(exc) == "frozen" else
                "The policy the agent used doesn't match the guardian's signed policy, so the guardian "
                "should decide."
            )
        except (LLMUnavailable, BudgetExhausted, PlatformAPIError) as exc:
            log.add("auditor_fail_closed", type(exc).__name__)
            logger.warning("Auditor failed closed (flag): %s: %s", type(exc).__name__, exc)
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
