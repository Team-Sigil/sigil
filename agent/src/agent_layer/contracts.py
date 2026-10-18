"""Typed contracts at the agent zone's boundaries.

Three groups:

* **Platform API payloads** – what the agent reads from and sends to the platform API.
  These mirror the policy engine's API and must be agreed with whoever builds it.
* **Tool arguments** – what the model may put in a tool call. Every model is
  ``extra="forbid"``, so a model cannot smuggle in a field such as a recipient.
* **Auditor outputs** – the Auditor's two structured answers.

All money in platform payloads is integer base units. Tool arguments carry display
amounts (max 2 decimals) and are converted by ``agent_layer.money``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_layer.money import AmountError, to_base


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Lenient(BaseModel):
    """Responses from the platform: ignore unknown fields so the API can grow."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class Category(StrEnum):
    EDUCATION = "education"
    FOOD = "food"
    TRANSPORT = "transport"
    HEALTH = "health"
    CLOTHING = "clothing"
    ENTERTAINMENT = "entertainment"
    GAMING = "gaming"
    SAVINGS = "savings"
    ALLOWANCE = "allowance"
    OTHER = "other"


class Outcome(StrEnum):
    SENT_FULL = "sent_full"
    SENT_PARTIAL = "sent_partial"
    COUNTER_OFFER = "counter_offer"
    ASKED_PROOF = "asked_proof"
    ASKED_GUARDIAN = "asked_guardian"
    DECLINED = "declined"
    # Set by code only, never chosen by the model:
    AUDITOR_FLAGGED = "auditor_flagged"
    BLOCKED = "blocked"
    SAFE_FALLBACK = "safe_fallback"


MODEL_CHOOSABLE_OUTCOMES = {
    Outcome.SENT_FULL,
    Outcome.SENT_PARTIAL,
    Outcome.COUNTER_OFFER,
    Outcome.ASKED_PROOF,
    Outcome.ASKED_GUARDIAN,
    Outcome.DECLINED,
}


# --------------------------------------------------------------------------- reads


class AllowanceRule(_Lenient):
    amount: int = Field(gt=0)
    weekday: int = Field(ge=0, le=6)  # 0 = Monday


class PolicyView(_Lenient):
    """The signed policy as the engine exposes it to the agent (read-only)."""

    version: int
    policy_hash: str
    weekly_cap: int = Field(ge=0)
    max_per_request: int = Field(ge=0)
    blocked_categories: list[Category] = []
    proof_threshold: int = Field(ge=0)
    review_threshold: int | None = Field(default=None, ge=0)
    allowance: AllowanceRule | None = None
    guidance: str = ""


class Budget(_Lenient):
    week_start: date
    spent_this_week: int = Field(ge=0)
    remaining_this_week: int = Field(ge=0)


class HistoryItem(_Lenient):
    at: datetime
    kind: Literal["transfer", "declined", "blocked", "asked_guardian", "auditor_flagged"]
    amount: int = Field(ge=0)
    category: Category
    purpose: str = ""


class AgentContext(_Lenient):
    dependent_id: str
    guardian_id: str
    dependent_name: str
    policy: PolicyView
    budget: Budget
    delegated_amount: int = Field(ge=0)
    frozen: bool = False
    history: list[HistoryItem] = []


class ThreadMessage(_Lenient):
    role: Literal["dependent", "agent", "guardian_answer", "system_event"]
    text: str
    at: datetime


class ProofRef(_Lenient):
    id: str
    media_type: str
    kind: Literal["receipt", "quote", "photo"] = "receipt"


class DependentRequest(_Lenient):
    id: str
    dependent_id: str
    message: str
    amount_requested: int | None = Field(default=None, gt=0)
    emergency_claimed: bool = False
    already_sent: int = Field(default=0, ge=0)
    thread: list[ThreadMessage] = []
    proofs: list[ProofRef] = []


class Proof(_Lenient):
    id: str
    media_type: str
    size_bytes: int = Field(ge=0)
    data_b64: str | None = None
    text: str | None = None


class DependentSummary(_Lenient):
    dependent_id: str
    guardian_id: str


class GuardianAnswer(_Lenient):
    question_id: str
    dependent_id: str
    category: Category
    answer: Literal["approved", "declined", "partial"]
    amount: int = Field(ge=0)
    at: datetime


class WeeklyActivity(_Lenient):
    dependent_id: str
    week_start: date
    items: list[HistoryItem] = []
    weekly_cap: int = Field(ge=0)


class PolicyDraftValidation(_Lenient):
    valid: bool
    errors: list[str] = []


# -------------------------------------------------------------------------- writes


class TransferInstruction(_Strict):
    """Sent to the engine. There is deliberately no recipient field: the engine
    resolves the dependent's registered token account itself."""

    dependent_id: str
    request_id: str | None
    trigger: Literal["request", "allowance"]
    amount: int = Field(gt=0)
    purpose: str
    category: Category
    category_confidence: float = Field(ge=0, le=1)
    reasoning: str
    idempotency_key: str
    policy_version: int
    auditor_verdict_id: str | None = None


class TransferResult(_Lenient):
    status: Literal["sent", "blocked", "duplicate"]
    decision_id: str
    amount: int = 0
    reason: str | None = None
    tx_signature: str | None = None


class IndependentAssessment(_Strict):
    stance: Literal["supports", "concerned", "unsure"]
    notes: str = Field(max_length=1_500)


class AuditorVerdict(_Strict):
    verdict: Literal["agree", "flag"]
    reason: str = Field(min_length=1, max_length=1_500)
    concerns: list[str] = Field(default_factory=list, max_length=10)


class VerdictSubmission(_Strict):
    """Posted with the Auditor's own credential, bound to one exact transfer."""

    dependent_id: str
    request_id: str | None
    idempotency_key: str
    amount: int = Field(gt=0)
    category: Category
    verdict: Literal["agree", "flag"]
    reason: str
    concerns: list[str]
    independent_assessment: IndependentAssessment
    review_triggers: list[str]
    model: str


class VerdictReceipt(_Lenient):
    verdict_id: str


class GuardianQuestion(_Strict):
    guardian_id: str
    dependent_id: str
    request_id: str | None
    question: str
    recommendation: Literal["approve", "approve_partial", "decline", "unsure", "top_up"]
    recommended_amount: int | None = None
    urgency: Literal["low", "normal", "high"] = "normal"
    agent_view: str
    auditor_view: str | None = None
    dedupe_key: str


class ProofRequest(_Strict):
    request_id: str
    dependent_id: str
    kind: Literal["receipt", "quote", "photo"]
    reason: str


class PolicyProposal(_Strict):
    guardian_id: str
    dependent_id: str
    change: str
    evidence: str
    dedupe_key: str


class Notification(_Strict):
    audience: Literal["dependent", "guardian"]
    recipient_id: str
    message: str
    dedupe_key: str | None = None


class DecisionRecord(_Strict):
    dependent_id: str
    request_id: str | None
    outcome: Outcome
    reply_to_dependent: str
    summary_for_guardian: str
    amount_sent: int = Field(ge=0)
    policy_version: int | None
    guardrail_events: list[str] = []


class AgentEvent(_Strict):
    kind: str
    subject_id: str | None = None
    detail: dict[str, Any] = {}


class UsageReport(_Strict):
    family_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class UsageStatus(_Lenient):
    remaining_tokens: int


class PolicyDraftSubmission(_Strict):
    guardian_id: str
    dependent_id: str
    draft: "PolicyDraft"


class Receipt(_Lenient):
    id: str = ""


# ------------------------------------------------------------------ tool arguments


def _display_amount(v: float) -> float:
    try:
        to_base(v)
    except AmountError as exc:
        raise ValueError(str(exc)) from exc
    if v <= 0:
        raise ValueError("amount must be positive")
    return v


class RequestTransferArgs(_Strict):
    amount: float = Field(description="Amount in tokens, e.g. 12.50. Max 2 decimals.")
    purpose: str = Field(min_length=3, max_length=200)
    category: Category
    category_confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=10, max_length=1_000)

    @field_validator("amount")
    @classmethod
    def _amt(cls, v: float) -> float:
        return _display_amount(v)


class AskForProofArgs(_Strict):
    kind: Literal["receipt", "quote", "photo"]
    reason: str = Field(min_length=3, max_length=300)


class AskGuardianArgs(_Strict):
    question: str = Field(min_length=5, max_length=500)
    recommendation: Literal["approve", "approve_partial", "decline", "unsure"]
    recommended_amount: float | None = Field(default=None)
    urgency: Literal["low", "normal", "high"] = "normal"

    @field_validator("recommended_amount")
    @classmethod
    def _amt(cls, v: float | None) -> float | None:
        return None if v is None else _display_amount(v)


class ProposePolicyUpdateArgs(_Strict):
    change: str = Field(min_length=5, max_length=300)
    evidence: str = Field(min_length=5, max_length=500)


class NotifyGuardianArgs(_Strict):
    message: str = Field(min_length=3, max_length=500)


class FinishArgs(_Strict):
    outcome: Literal[
        "sent_full", "sent_partial", "counter_offer", "asked_proof", "asked_guardian", "declined"
    ]
    reply_to_dependent: str = Field(min_length=1, max_length=1_200)
    summary_for_guardian: str = Field(min_length=1, max_length=400)


# ------------------------------------------------------------------- policy drafts


class TestScenario(_Strict):
    __test__ = False  # not a pytest class

    scenario: str = Field(max_length=300)
    what_i_would_do: str = Field(max_length=400)


class PolicyDraft(_Strict):
    """What the agent proposes from the guardian's chat. Amounts are display units;
    the guardian signs the engine's canonical form, never this object directly."""

    weekly_cap: float = Field(gt=0)
    max_per_request: float = Field(gt=0)
    blocked_categories: list[Category] = []
    proof_threshold: float = Field(ge=0)
    review_threshold: float | None = Field(default=None, ge=0)
    allowance_amount: float | None = Field(default=None, gt=0)
    allowance_weekday: int | None = Field(default=None, ge=0, le=6)
    guidance: str = Field(max_length=4_000)
    test_scenarios: list[TestScenario] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=8)


PolicyDraftSubmission.model_rebuild()
