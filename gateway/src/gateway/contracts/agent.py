"""The agent layer's platform API payloads, mirrored verbatim.

Source of truth: `agent_layer/contracts.py` in Team-Sigil/agent-layer (as of b5e5d25). The
gateway serves these shapes on `/agent/v1/*`. Do not edit by hand: change them in the agent
layer first, then copy. `tests/test_contracts.py` compares the JSON schemas with the agent
layer whenever it is checked out next to this repo.

Tool arguments and the Auditor's internal outputs stay in the agent layer; they never cross
the boundary.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Lenient(BaseModel):
    """Responses from the platform: ignore unknown fields so the API can grow."""

    model_config = ConfigDict(extra="ignore", frozen=True)


# Hard ceilings on platform responses. They sit well above the guardrail limits in
# ``config.Limits``, which truncate; a response over a ceiling is rejected outright and
# the run fails closed, because no honest platform sends it.
MAX_TEXT = 64_000  # a message, thread message or text proof
MAX_SHORT_TEXT = 2_000  # a purpose, reason, name or validation error
MAX_GUIDANCE = 16_000
MAX_ITEMS = 1_000  # history, activity and thread entries
MAX_PROOF_B64 = 8 * 1024 * 1024  # base64 of a proof at the 5 MB limit is ~6.7 MB


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
    ASKED_DEPENDENT = "asked_dependent"  # a clarifying question; nothing else happened
    DECLINED = "declined"
    OUT_OF_SCOPE = "out_of_scope"  # not about the allowance, e.g. "what is 1+1?"
    # Set by code only, never chosen by the model:
    AUDITOR_FLAGGED = "auditor_flagged"
    BLOCKED = "blocked"
    SAFE_FALLBACK = "safe_fallback"
    TRANSFER_PENDING = "transfer_pending"  # cleared, but the chain hasn't confirmed it yet


MODEL_CHOOSABLE_OUTCOMES = {
    Outcome.SENT_FULL,
    Outcome.SENT_PARTIAL,
    Outcome.COUNTER_OFFER,
    Outcome.ASKED_PROOF,
    Outcome.ASKED_GUARDIAN,
    Outcome.ASKED_DEPENDENT,
    Outcome.DECLINED,
    Outcome.OUT_OF_SCOPE,
}


# --------------------------------------------------------------------------- reads


class AllowanceRule(_Lenient):
    amount: int = Field(gt=0)
    weekday: int = Field(ge=0, le=6)  # 0 = Monday


class PolicyView(_Lenient):
    """The signed policy as the engine exposes it to the agent (read-only)."""

    version: int
    policy_hash: str = Field(max_length=200)
    weekly_cap: int = Field(ge=0)
    max_per_request: int = Field(ge=0)
    blocked_categories: list[Category] = []
    proof_threshold: int = Field(ge=0)
    review_threshold: int | None = Field(default=None, ge=0)
    allowance: AllowanceRule | None = None
    guidance: str = Field(default="", max_length=MAX_GUIDANCE)


class Budget(_Lenient):
    week_start: date
    spent_this_week: int = Field(ge=0)
    remaining_this_week: int = Field(ge=0)


class HistoryItem(_Lenient):
    at: datetime
    kind: Literal["transfer", "declined", "blocked", "asked_guardian", "auditor_flagged", "out_of_scope"]
    amount: int = Field(ge=0)
    category: Category
    purpose: str = Field(default="", max_length=MAX_SHORT_TEXT)


class AgentContext(_Lenient):
    dependent_id: str
    guardian_id: str
    dependent_name: str = Field(max_length=MAX_SHORT_TEXT)
    policy: PolicyView
    budget: Budget
    delegated_amount: int = Field(ge=0)
    frozen: bool = False
    history: list[HistoryItem] = Field(default=[], max_length=MAX_ITEMS)
    # BCP 47 tags from the users' profiles, e.g. "en", "zh-CN", "zh-TW" (see i18n.py).
    dependent_locale: str | None = Field(default=None, max_length=35)
    guardian_locale: str | None = Field(default=None, max_length=35)


class ThreadMessage(_Lenient):
    role: Literal["dependent", "agent", "guardian_answer", "system_event"]
    text: str = Field(max_length=MAX_TEXT)
    at: datetime


class ProofRef(_Lenient):
    id: str
    media_type: str
    kind: Literal["receipt", "quote", "photo"] = "receipt"


class DependentRequest(_Lenient):
    id: str
    dependent_id: str
    message: str = Field(max_length=MAX_TEXT)
    amount_requested: int | None = Field(default=None, gt=0)
    emergency_claimed: bool = False
    already_sent: int = Field(default=0, ge=0)
    # Cleared by the engine but not yet confirmed on chain: counts as sent for "never
    # more than asked", because it may still go out.
    already_pending: int = Field(default=0, ge=0)
    # The amount the guardian approved for this request with a wallet signature, as
    # verified by the platform. Exactly this amount may go above the per-request max and
    # the weekly cap without review; the engine re-verifies the signature itself.
    guardian_approved_amount: int | None = Field(default=None, gt=0)
    thread: list[ThreadMessage] = Field(default=[], max_length=MAX_ITEMS)
    proofs: list[ProofRef] = Field(default=[], max_length=50)


# Where a proof came from. "photo" means a verified in-app capture (signed on the
# device); "file" anything else. Typed text is the dependent's own claim, never evidence.
ProofSource = Literal["photo", "file", "ocr", "typed"]
# Integrity flags the platform may set at upload (see proof-images.md). Anything else
# is ignored, so no free text from the platform reaches the prompt this way.
PROOF_FLAGS_DISQUALIFYING = frozenset({"duplicate", "ai_generated"})  # never counts as proof
PROOF_FLAGS_POSITIVE = frozenset({"camera_signed"})  # a valid C2PA camera signature: a note only
PROOF_FLAGS = PROOF_FLAGS_DISQUALIFYING | PROOF_FLAGS_POSITIVE | frozenset({
    "near_duplicate", "screenshot", "no_camera_metadata", "old_capture", "edited",
    "ai_suspected", "rephotographed_screen",
})
# Flags that force an Auditor review.
PROOF_FLAGS_CONCERNS = PROOF_FLAGS - PROOF_FLAGS_POSITIVE


class Proof(_Lenient):
    id: str
    media_type: str
    size_bytes: int = Field(ge=0)
    data_b64: str | None = Field(default=None, max_length=MAX_PROOF_B64)
    text: str | None = Field(default=None, max_length=MAX_TEXT)
    # Integrity metadata, set by the platform at upload (see proof-images.md). A proof
    # without a matching request_id and dependent_id is shown but never counts as proof.
    request_id: str | None = Field(default=None, max_length=64)
    dependent_id: str | None = Field(default=None, max_length=64)
    uploaded_at: datetime | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: ProofSource | None = None
    # For ``ocr`` text: the id of the image proof it was read from.
    derived_from: str | None = Field(default=None, max_length=64)
    flags: list[Annotated[str, Field(max_length=40)]] = Field(default=[], max_length=10)


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
    items: list[HistoryItem] = Field(default=[], max_length=MAX_ITEMS)
    weekly_cap: int = Field(ge=0)


class PolicyDraftValidation(_Lenient):
    valid: bool
    errors: list[Annotated[str, Field(max_length=MAX_SHORT_TEXT)]] = Field(default=[], max_length=50)


# -------------------------------------------------------------------------- writes


class ProofFindingsRecord(_Strict):
    """``ProofFindings`` as sent to the engine, with the total in base units."""

    merchant: str
    date: str
    total: int = Field(ge=0)
    receipt_number: str | None = None
    matches_claim: bool


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
    # The proofs this transfer relies on (chosen by code, not the model), and what the
    # model read from them. The engine requires an unused proof above the threshold and
    # marks these used when it sends.
    proof_ids: list[str] = []
    proof_findings: ProofFindingsRecord | None = None


class TransferResult(_Lenient):
    # "pending": cleared and reserved, but the chain hasn't confirmed it (e.g. the signer
    # is down). It may still go out, so it is never reported as blocked.
    status: Literal["sent", "blocked", "duplicate", "pending"]
    decision_id: str
    amount: int = 0
    reason: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
    tx_signature: str | None = None


class IndependentAssessment(_Strict):
    stance: Literal["supports", "concerned", "unsure"]
    notes: str = Field(max_length=1_500)


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
