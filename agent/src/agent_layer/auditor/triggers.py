"""When a transfer needs the Auditor's review.

Split in two on purpose:

* ``trusted_triggers`` uses only facts the engine also holds (amount, signed policy,
  history, the gateway's emergency flag). The engine runs the same rule and refuses a
  transfer that needs review but carries no valid verdict, so a manipulated maker
  cannot skip review by lying.
* ``agent_triggers`` adds signals only the agent zone sees (the model's category
  confidence, injection signals). They can only add reviews, never remove one.
"""

from __future__ import annotations

from statistics import median
from typing import Literal

from agent_layer.contracts import Category, HistoryItem, PolicyView

LOW_CONFIDENCE = 0.7
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


def agent_triggers(*, category_confidence: float, injection_signals: list[str]) -> list[str]:
    reasons: list[str] = []
    if category_confidence < LOW_CONFIDENCE:
        reasons.append("low_category_confidence")
    if injection_signals:
        reasons.append("injection_signals")
    return reasons
