import inspect
from enum import Enum
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from gateway.contracts import agent
from gateway.contracts.agent import Category, PolicyDraft
from gateway.contracts.crypto import CryptoTransferRequest, Delegation
from gateway.contracts.policy import PolicyBody, body_from_draft, policy_hash, policy_view
from gateway.contracts.signing import (
    activate_policy_message,
    answer_question_message,
    freeze_message,
    register_dependent_message,
)
from gateway.enums import GuardianDecision
from tests.agent_layer import import_agent_layer

AUSD = 1_000_000


def body(**overrides: Any) -> PolicyBody:
    """The agent layer's demo family policy (mock_platform/seed.py) in base units."""
    values: dict[str, Any] = {
        "weekly_cap": 100 * AUSD,
        "max_per_request": 40 * AUSD,
        "blocked_categories": ["gaming"],
        "proof_threshold": 25 * AUSD,
        "allowance": {"amount": 30 * AUSD, "weekday": 0},
        "guidance": "School supplies and bus fare are always fine.",
    }
    values.update(overrides)
    return PolicyBody.model_validate(values)


# --- agent boundary -------------------------------------------------------------------------


def _agent_layer_contracts() -> ModuleType:
    return import_agent_layer("agent_layer.contracts")


def _mirrored(module: ModuleType) -> dict[str, type]:
    return {
        name: obj
        for name, obj in vars(module).items()
        if inspect.isclass(obj)
        and obj.__module__ == module.__name__
        and issubclass(obj, BaseModel | Enum)
        and not name.startswith("_")
    }


def test_agent_contracts_match_the_agent_layer() -> None:
    """Our /agent/v1 payloads must be exactly what the agent layer sends and expects."""
    theirs = _agent_layer_contracts()
    ours = _mirrored(agent)
    assert ours, "nothing mirrored"
    for name, model in ours.items():
        other = getattr(theirs, name, None)
        assert other is not None, f"{name} no longer exists in the agent layer"
        if issubclass(model, Enum):
            assert [m.value for m in model] == [m.value for m in other], name
        else:
            assert issubclass(model, BaseModel) and issubclass(other, BaseModel)
            assert model.model_json_schema() == other.model_json_schema(), name
    for constant in ("MAX_TEXT", "MAX_SHORT_TEXT", "MAX_GUIDANCE", "MAX_ITEMS", "MAX_PROOF_B64"):
        assert getattr(agent, constant) == getattr(theirs, constant), constant
    assert agent.PROOF_FLAGS == theirs.PROOF_FLAGS


def test_transfer_instruction_has_no_recipient_field() -> None:
    """SEC-01: nothing the agent sends can name where money goes."""
    fields = set(agent.TransferInstruction.model_fields)
    assert not {f for f in fields if "recipient" in f or "destination" in f or "address" in f}
    with pytest.raises(ValidationError):
        agent.TransferInstruction.model_validate(
            {
                "dependent_id": "d1",
                "request_id": "r1",
                "trigger": "request",
                "amount": 1,
                "purpose": "x",
                "category": "food",
                "category_confidence": 0.9,
                "reasoning": "x",
                "idempotency_key": "k",
                "policy_version": 1,
                "destination_token_account": "attacker",
            }
        )


# --- policy -----------------------------------------------------------------------------------


class TestPolicyBody:
    def test_demo_policy_is_valid(self) -> None:
        assert body().allowance is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"max_per_request": 101 * AUSD},
            {"proof_threshold": 41 * AUSD},
            {"review_threshold": 41 * AUSD},
            {"allowance": {"amount": 101 * AUSD, "weekday": 0}},
            {"allowance": {"amount": 1, "weekday": 7}},
            {"blocked_categories": ["gaming", "gaming"]},
            {"blocked_categories": ["allowance"]},
            {"blocked_categories": ["crypto"]},
            {"weekly_cap": -1},
            {"guidance": "x" * 4_001},
            {"daily_cap": 1},
        ],
    )
    def test_invalid_bodies_rejected(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            body(**overrides)

    @pytest.mark.parametrize("amount", [1.5, 100.0, "100", True])
    def test_money_must_be_a_strict_int(self, amount: object) -> None:
        with pytest.raises(ValidationError):
            body(weekly_cap=amount)

    def test_hash_is_stable_and_sensitive(self) -> None:
        assert policy_hash(body()) == policy_hash(body())
        assert policy_hash(body()) != policy_hash(body(weekly_cap=101 * AUSD))
        assert len(policy_hash(body())) == 64

    def test_view_matches_what_the_agent_reads(self) -> None:
        view = policy_view(body(), version=3, body_hash="h" * 64)
        assert view.max_per_request == 40 * AUSD
        assert view.allowance is not None and view.allowance.amount == 30 * AUSD
        assert view.review_threshold is None
        assert view.version == 3


class TestBodyFromDraft:
    def draft(self, **overrides: Any) -> PolicyDraft:
        values: dict[str, Any] = {
            "weekly_cap": 100.0,
            "max_per_request": 40.0,
            "blocked_categories": ["gaming"],
            "proof_threshold": 25.0,
            "allowance_amount": 30.0,
            "allowance_weekday": 0,
            "guidance": "Books are fine.",
        }
        values.update(overrides)
        return PolicyDraft.model_validate(values)

    def test_converts_display_units_exactly(self) -> None:
        converted = body_from_draft(self.draft(weekly_cap=100.1, max_per_request=40.35))
        assert converted.weekly_cap == 100_100_000
        assert converted.max_per_request == 40_350_000
        assert converted.allowance is not None and converted.allowance.amount == 30 * AUSD

    def test_more_than_two_decimals_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2 decimal"):
            body_from_draft(self.draft(max_per_request=1.005))

    def test_allowance_needs_a_weekday(self) -> None:
        with pytest.raises(ValueError, match="weekday"):
            body_from_draft(self.draft(allowance_weekday=None))

    def test_inconsistent_draft_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            body_from_draft(self.draft(max_per_request=200.0))

    def test_category_enum_is_the_agents(self) -> None:
        assert Category.ALLOWANCE in Category


# --- crypto boundary --------------------------------------------------------------------------


def test_crypto_transfer_rejects_extra_fields_and_non_positive_amounts() -> None:
    base = {
        "transfer_id": str(uuid4()),
        "family_id": str(uuid4()),
        "amount_units": 8 * AUSD,
        "source_token_account": "src",
        "destination_token_account": "dst",
        "mint": "mint",
        "idempotency_key": "req:r1:1",
    }
    assert CryptoTransferRequest.model_validate(base).amount_units == 8 * AUSD
    for bad in ({"amount_units": 0}, {"amount_units": 1.5}, {"memo": "hi"}):
        with pytest.raises(ValidationError):
            CryptoTransferRequest.model_validate({**base, **bad})


def test_delegation_amount_is_a_strict_int() -> None:
    with pytest.raises(ValidationError):
        Delegation.model_validate({"token_account": "a", "delegated_amount_units": 1.5})


# --- signed messages (SPEC §7.5) --------------------------------------------------------------

FAMILY = UUID("11111111-1111-4111-8111-111111111111")
QUESTION = UUID("22222222-2222-4222-8222-222222222222")


def test_activate_policy_message() -> None:
    assert activate_policy_message(FAMILY, 3, "ab" * 32, "n1") == (
        f"FinGuard:activate-policy\nfamily:{FAMILY}\nversion:3\nhash:{'ab' * 32}\nnonce:n1"
    )


def test_answer_question_message_with_and_without_amount() -> None:
    approve = answer_question_message(FAMILY, QUESTION, GuardianDecision.APPROVE, 5, "n")
    deny = answer_question_message(FAMILY, QUESTION, GuardianDecision.DENY, None, "n")
    assert approve.splitlines()[3:5] == ["decision:approve", "amount_units:5"]
    assert deny.splitlines()[3:5] == ["decision:deny", "amount_units:none"]


def test_freeze_and_register_messages() -> None:
    assert freeze_message(FAMILY, True, "n") == (
        f"FinGuard:freeze\nfamily:{FAMILY}\nfrozen:true\nnonce:n"
    )
    wallet = "So11111111111111111111111111111111111111112"
    register = register_dependent_message(None, wallet, wallet, "n")
    assert register.splitlines()[:2] == ["FinGuard:register-dependent", "family:new"]
