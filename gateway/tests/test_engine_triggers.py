import importlib
import itertools
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gateway.contracts import agent
from gateway.engine.triggers import review_threshold, trusted_triggers

AUSD = 1_000_000
AGENT_LAYER_SRC = Path(__file__).resolve().parents[2] / "agent-layer" / "src"


def policy(review: int | None = None, allowance: int | None = 30 * AUSD) -> dict[str, Any]:
    return {
        "version": 3,
        "policy_hash": "h",
        "weekly_cap": 100 * AUSD,
        "max_per_request": 40 * AUSD,
        "proof_threshold": 25 * AUSD,
        "review_threshold": review,
        "allowance": None if allowance is None else {"amount": allowance, "weekday": 0},
    }


def history(amounts: list[int], category: str = "food") -> list[dict[str, Any]]:
    at = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    return [{"at": at, "kind": "transfer", "amount": a, "category": category} for a in amounts]


def run(module: ModuleType, triggers: Any, case: dict[str, Any]) -> list[str]:
    result: list[str] = triggers(
        amount=case["amount"],
        category=module.Category(case["category"]),
        policy=module.PolicyView.model_validate(case["policy"]),
        history=[module.HistoryItem.model_validate(h) for h in case["history"]],
        trigger=case["trigger"],
        emergency_claimed=case["emergency"],
    )
    return result


GRID = [
    {
        "amount": amount,
        "category": category,
        "policy": pol,
        "history": hist,
        "trigger": trigger,
        "emergency": emergency,
    }
    for amount, category, pol, hist, trigger, emergency in itertools.product(
        [1, 8 * AUSD, 24_999_999, 25 * AUSD, 30 * AUSD, 31 * AUSD],
        ["food", "education", "allowance"],
        [policy(), policy(review=10 * AUSD), policy(allowance=None)],
        [
            [],
            history([5 * AUSD] * 2),
            history([5 * AUSD] * 3),
            history([2 * AUSD, 4 * AUSD, 20 * AUSD]),
        ],
        ["request", "allowance"],
        [False, True],
    )
]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ({"amount": 24_999_999, "history": []}, []),
        ({"amount": 25 * AUSD, "history": []}, ["over_review_threshold"]),
        ({"emergency": True}, ["emergency_claimed"]),
        ({"history": history([3 * AUSD] * 3)}, ["off_pattern_amount"]),
        ({"history": history([5 * AUSD] * 3, category="transport")}, ["new_category"]),
        ({"trigger": "allowance", "amount": 30 * AUSD}, []),
        ({"trigger": "allowance", "amount": 30 * AUSD + 1}, ["allowance_above_usual"]),
    ],
)
def test_fixed_cases(case: dict[str, Any], expected: list[str]) -> None:
    base = {
        "amount": 8 * AUSD,
        "category": "food",
        "policy": policy(),
        "history": history([5 * AUSD] * 3),
        "trigger": "request",
        "emergency": False,
    }
    assert run(agent, trusted_triggers, base | case) == expected


def test_default_review_threshold_is_a_quarter_of_the_cap() -> None:
    assert review_threshold(agent.PolicyView.model_validate(policy())) == 25 * AUSD
    assert review_threshold(agent.PolicyView.model_validate(policy(review=7))) == 7


def test_engine_and_agent_agree_on_every_case() -> None:
    """A difference would let a manipulated maker skip a review the engine should demand,
    or make the engine block transfers the agent thinks are fine."""
    if not AGENT_LAYER_SRC.is_dir():
        pytest.skip("agent-layer is not checked out next to this repo")
    sys.path.insert(0, str(AGENT_LAYER_SRC))
    try:
        their_contracts = importlib.import_module("agent_layer.contracts")
        their_triggers = importlib.import_module("agent_layer.auditor.triggers")
    finally:
        sys.path.remove(str(AGENT_LAYER_SRC))

    for case in GRID:
        ours = run(agent, trusted_triggers, case)
        theirs = run(their_contracts, their_triggers.trusted_triggers, case)
        assert ours == theirs, case
