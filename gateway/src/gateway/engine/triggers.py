"""When a transfer needs an Auditor verdict: the agent layer's `trusted_triggers`, copied.

Source of truth: `agent_layer/auditor/triggers.py` (as of 72b80be). The engine must apply the
same rule as the agent, or a manipulated maker could skip review by lying about it.
`tests/test_engine_triggers.py` compares both on a grid of cases when the agent layer is
checked out. Only the engine-verifiable half is copied; `agent_triggers` can only add reviews.
"""

from __future__ import annotations

from statistics import median
from typing import Literal

from gateway.contracts.agent import Category, HistoryItem, PolicyView

OFF_PATTERN_FACTOR = 2.0
MIN_HISTORY_FOR_PATTERN = 3
DEFAULT_REVIEW_FRACTION = 0.25


def review_threshold(policy: PolicyView, default_fraction: float = DEFAULT_REVIEW_FRACTION) -> int:
    """The guardian's own threshold when set; otherwise a share of the weekly cap."""
    if policy.review_threshold is not None:
        return policy.review_threshold
    return int(policy.weekly_cap * default_fraction)


def trusted_triggers(
    *,
    amount: int,
    category: Category,
    policy: PolicyView,
    history: list[HistoryItem],
    trigger: Literal["request", "allowance"],
    emergency_claimed: bool = False,
) -> list[str]:
    if trigger == "allowance":
        usual = policy.allowance.amount if policy.allowance else 0
        return ["allowance_above_usual"] if amount > usual else []

    reasons: list[str] = []
    if amount >= review_threshold(policy):
        reasons.append("over_review_threshold")
    if emergency_claimed:
        reasons.append("emergency_claimed")
    past = [h for h in history if h.kind == "transfer" and h.category is not Category.ALLOWANCE]
    if len(past) >= MIN_HISTORY_FOR_PATTERN:
        if amount > OFF_PATTERN_FACTOR * median(h.amount for h in past):
            reasons.append("off_pattern_amount")
        if category not in {h.category for h in past}:
            reasons.append("new_category")
    return reasons
