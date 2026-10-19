"""Turn the guardian's plain-language chat into a policy draft.

The agent only *drafts*. The draft is sanity-checked locally, then validated by the
engine; the guardian signs the engine's canonical form in their wallet. Neither this
code nor the model can activate a policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_layer.config import Limits
from agent_layer.contracts import PolicyDraft, PolicyDraftSubmission, PolicyDraftValidation
from agent_layer.guardrails.budget import TokenBudget
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import sanitize, wrap_untrusted
from agent_layer.llm import LLM, LLMUnavailable
from agent_layer.platform_api import MakerAPI

logger = logging.getLogger(__name__)

POLICY_SYSTEM = """\
You help a guardian set up a weekly allowance for their dependent. From the
conversation, draft a policy:

- A few outer limits that code enforces: weekly cap, max per request, blocked
  categories, the amount from which proof (a receipt) is needed, an optional review
  threshold for the independent Auditor, and an optional regular allowance (amount and
  weekday, 0 = Monday). Amounts are tokens with at most 2 decimals.
- Guidance: everything else the guardian said, in their spirit, written as advice to
  the agent that will run the allowance (what is always fine, never fine, when to ask
  first, context about the dependent).
- Up to 5 test scenarios: "if the dependent asks for X, I would do Y", so the guardian
  can correct you before real requests arrive.
- Open questions for anything you had to guess.

Only use what the guardian actually said; where a limit was not given, choose a
conservative value and list it under open questions.

Write the guidance, test scenarios and open questions in the language the guardian
writes in (English or Chinese, keeping their Simplified or Traditional script).
"""


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["guardian", "agent"]
    text: str = Field(max_length=4_000)
    # For agent turns in a policy interview: what the turn was (question_kind in the
    # previous InterviewTurn). The gateway stores it and sends it back.
    kind: Literal["question", "confirmation", "review"] | None = None


@dataclass
class DraftResult:
    draft: PolicyDraft | None
    validation: PolicyDraftValidation
    events: list[str]


def local_checks(d: PolicyDraft) -> list[str]:
    errors = []
    if d.max_per_request > d.weekly_cap:
        errors.append("max_per_request is larger than weekly_cap")
    if d.proof_threshold > d.max_per_request:
        errors.append("proof_threshold is larger than max_per_request, so proof would never be needed")
    if d.allowance_amount is not None and d.allowance_amount > d.weekly_cap:
        errors.append("allowance_amount is larger than weekly_cap")
    if (d.allowance_amount is None) != (d.allowance_weekday is None):
        errors.append("allowance_amount and allowance_weekday must be set together")
    return errors


def draft_policy(
    llm: LLM,
    api: MakerAPI,
    *,
    guardian_id: str,
    dependent_id: str,
    conversation: list[ChatTurn],
    limits: Limits | None = None,
) -> DraftResult:
    limits = limits or Limits()
    log = GuardrailLog()
    budget = TokenBudget(api, guardian_id, log)
    transcript = []
    for turn in conversation[-limits.max_thread_messages :]:
        text = sanitize(turn.text, max_chars=4_000, log=log, source=turn.role).text
        transcript.append(wrap_untrusted(f"{turn.role}_message", text))
    messages: list[dict] = [{"role": "user", "content": "\n".join(transcript) + "\n\nDraft the policy."}]

    for attempt in range(2):  # one repair round if the draft fails validation
        budget.ensure_available()
        try:
            out = llm.parse(system=POLICY_SYSTEM, messages=messages, output_format=PolicyDraft)
        except LLMUnavailable as exc:
            log.add("draft_unavailable")
            logger.warning("policy draft unavailable: %s", exc)
            return DraftResult(None, PolicyDraftValidation(valid=False, errors=["model unavailable"]), log.events)
        budget.record(out.response)
        draft = out.parsed
        if draft is None or out.response.stop_reason == "refusal":
            log.add("draft_unparseable")
            return DraftResult(None, PolicyDraftValidation(valid=False, errors=["no draft produced"]), log.events)

        errors = local_checks(draft)
        validation = (
            PolicyDraftValidation(valid=False, errors=errors)
            if errors
            else api.validate_policy_draft(
                PolicyDraftSubmission(guardian_id=guardian_id, dependent_id=dependent_id, draft=draft)
            )
        )
        if validation.valid or attempt == 1:
            return DraftResult(draft, validation, log.events)
        log.add("draft_repair", "; ".join(validation.errors))
        messages += [
            {"role": "assistant", "content": out.response.content},
            {"role": "user", "content": "The draft failed validation: " + "; ".join(validation.errors) + ". Fix it."},
        ]
    raise AssertionError("unreachable")
