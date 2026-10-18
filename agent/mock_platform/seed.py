"""A demo family: guardian g1, dependent d1 (Mali)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from agent_layer.contracts import AllowanceRule, Category, HistoryItem, PolicyView
from agent_layer.money import to_base
from mock_platform.app import DependentState, MockPlatform

GUIDANCE = (
    "Mali is 15 and in grade 10. School supplies, books and bus fare are always fine. "
    "Snacks with friends are fine in moderation. No money for mobile game top-ups or loot boxes. "
    "Ask me first about anything over 30 or anything to do with concerts or trips. "
    "She is saving for a bicycle, so gently encourage saving."
)


def seed_family(platform: MockPlatform, *, week_start: date = date(2026, 10, 12), delegated: float = 500) -> None:
    policy = PolicyView(
        version=3,
        policy_hash="sha256:demo",
        weekly_cap=to_base(100),
        max_per_request=to_base(40),
        blocked_categories=[Category.GAMING],
        proof_threshold=to_base(25),
        review_threshold=None,  # default: 25% of the weekly cap = 25.00
        allowance=AllowanceRule(amount=to_base(30), weekday=0),
        guidance=GUIDANCE,
    )

    def at(days_ago: int) -> datetime:
        return datetime.combine(week_start, datetime.min.time(), tzinfo=UTC) - timedelta(days=days_ago)

    history = [
        HistoryItem(at=at(12), kind="transfer", amount=to_base(8), category=Category.FOOD, purpose="lunch"),
        HistoryItem(at=at(10), kind="transfer", amount=to_base(12), category=Category.EDUCATION, purpose="notebooks"),
        HistoryItem(at=at(6), kind="transfer", amount=to_base(10), category=Category.TRANSPORT, purpose="bus card"),
        HistoryItem(at=at(3), kind="transfer", amount=to_base(9), category=Category.FOOD, purpose="snacks"),
    ]
    platform.add_dependent(
        DependentState(
            dependent_id="d1",
            guardian_id="g1",
            name="Mali",
            policy=policy,
            week_start=week_start,
            spent_this_week=to_base(30),  # this week's allowance already went out
            history=history,
        ),
        delegated=to_base(delegated),
    )
