"""The canonical policy: what the guardian signs and what the engine enforces.

Amounts are integer base units. The agent drafts a policy in display units
(`agent.PolicyDraft`); `body_from_draft` converts it exactly, the guardian signs the hash of
the resulting body, and the agent later reads it back as a `PolicyView`.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gateway.contracts.agent import AllowanceRule, Category, PolicyDraft, PolicyView
from gateway.core.crypto import canonical_json, sha256_hex
from gateway.money import to_base

MAX_GUIDANCE_CHARS = 4_000  # what the agent drafts (PolicyDraft.guidance)

# Strict ints: floats, numeric strings and bools are rejected, so no float enters a money path.
Units = Annotated[int, Field(strict=True, ge=0)]


class Allowance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Annotated[int, Field(strict=True, gt=0)]
    weekday: int = Field(strict=True, ge=0, le=6)  # 0 = Monday


class PolicyBody(BaseModel):
    """Stored as `policies.body`; its hash is what the guardian's wallet signs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    currency: Literal["aUSD"] = "aUSD"
    weekly_cap: Units
    max_per_request: Units
    blocked_categories: list[Category] = []
    proof_threshold: Units
    review_threshold: Units | None = None  # None: the engine uses 25% of the weekly cap
    allowance: Allowance | None = None
    guidance: str = Field(default="", max_length=MAX_GUIDANCE_CHARS)

    @model_validator(mode="after")
    def check_consistency(self) -> Self:
        if self.max_per_request > self.weekly_cap:
            raise ValueError("max_per_request must not exceed weekly_cap")
        if self.proof_threshold > self.max_per_request:
            raise ValueError("proof_threshold must not exceed max_per_request")
        if self.review_threshold is not None and self.review_threshold > self.max_per_request:
            raise ValueError("review_threshold must not exceed max_per_request")
        if self.allowance is not None and self.allowance.amount > self.weekly_cap:
            raise ValueError("allowance amount must not exceed weekly_cap")
        if len(set(self.blocked_categories)) != len(self.blocked_categories):
            raise ValueError("blocked_categories must not repeat")
        if Category.ALLOWANCE in self.blocked_categories:
            raise ValueError("the allowance category cannot be blocked")
        return self


def policy_hash(body: PolicyBody) -> str:
    """SHA-256 hex of the canonical JSON of the body (SPEC §6.4, §7)."""
    return sha256_hex(canonical_json(body.model_dump(mode="json")))


def body_from_draft(draft: PolicyDraft) -> PolicyBody:
    """Exact display-to-base conversion of an agent draft. Raises `ValueError` (from `to_base`
    or validation) when the draft cannot become a valid policy."""
    allowance = None
    if draft.allowance_amount is not None:
        if draft.allowance_weekday is None:
            raise ValueError("allowance_weekday is required with allowance_amount")
        allowance = Allowance(
            amount=to_base(draft.allowance_amount), weekday=draft.allowance_weekday
        )
    return PolicyBody(
        weekly_cap=to_base(draft.weekly_cap),
        max_per_request=to_base(draft.max_per_request),
        blocked_categories=list(draft.blocked_categories),
        proof_threshold=to_base(draft.proof_threshold),
        review_threshold=(
            None if draft.review_threshold is None else to_base(draft.review_threshold)
        ),
        allowance=allowance,
        guidance=draft.guidance,
    )


def policy_view(body: PolicyBody, version: int, body_hash: str) -> PolicyView:
    """The signed policy as the agent reads it (`AgentContext.policy`)."""
    return PolicyView(
        version=version,
        policy_hash=body_hash,
        weekly_cap=body.weekly_cap,
        max_per_request=body.max_per_request,
        blocked_categories=body.blocked_categories,
        proof_threshold=body.proof_threshold,
        review_threshold=body.review_threshold,
        allowance=(
            None
            if body.allowance is None
            else AllowanceRule(amount=body.allowance.amount, weekday=body.allowance.weekday)
        ),
        guidance=body.guidance,
    )
