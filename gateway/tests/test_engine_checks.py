from datetime import UTC, datetime
from typing import Any

import pytest

from gateway.contracts.agent import HistoryItem, Proof, ProofFindingsRecord, TransferInstruction
from gateway.contracts.policy import PolicyBody
from gateway.engine.checks import (
    RequestFacts,
    TransferFacts,
    VerdictFacts,
    check_transfer,
    owes_proof_after,
)

AUSD = 1_000_000
DEP = "d1"
ALLOWANCE_KEY = "allowance:d1:2026-W42"

# The agent layer's demo policy: cap 100, max 40, proof 25, review 25 (default), allowance 30.
POLICY = PolicyBody(
    weekly_cap=100 * AUSD,
    max_per_request=40 * AUSD,
    blocked_categories=["gaming"],
    proof_threshold=25 * AUSD,
    allowance={"amount": 30 * AUSD, "weekday": 0},
)


def instruction(**overrides: Any) -> TransferInstruction:
    values: dict[str, Any] = {
        "dependent_id": DEP,
        "request_id": "r1",
        "trigger": "request",
        "amount": 8 * AUSD,
        "purpose": "notebooks",
        "category": "education",
        "category_confidence": 0.9,
        "reasoning": "fits the guidance",
        "idempotency_key": "req:r1:1",
        "policy_version": 3,
    }
    values.update(overrides)
    return TransferInstruction.model_validate(values)


def allowance(**overrides: Any) -> TransferInstruction:
    values: dict[str, Any] = {
        "request_id": None,
        "trigger": "allowance",
        "amount": 30 * AUSD,
        "category": "allowance",
        "idempotency_key": ALLOWANCE_KEY,
    }
    values.update(overrides)
    return instruction(**values)


def proof(**overrides: Any) -> Proof:
    values: dict[str, Any] = {
        "id": "p1",
        "media_type": "image/jpeg",
        "size_bytes": 10,
        "request_id": "r1",
        "dependent_id": DEP,
        "sha256": "a" * 64,
        "source": "photo",
    }
    values.update(overrides)
    return Proof.model_validate(values)


FINDINGS = ProofFindingsRecord(
    merchant="Book Shop", date="2026-10-14", total=30 * AUSD, matches_claim=True
)


def facts(**overrides: Any) -> TransferFacts:
    """Facts that pass every check; tests override one thing at a time."""
    values: dict[str, Any] = {
        "instruction": instruction(),
        "family_found": True,
        "frozen": False,
        "policy": POLICY,
        "policy_version": 3,
        "policy_signature_valid": True,
        "destination_registered": True,
        "spent": 0,
        "reserved": 0,
        "reservoir_reserved": 0,
        "delegated_remaining": 500 * AUSD,
        "expected_allowance_key": ALLOWANCE_KEY,
        "request": RequestFacts(
            dependent_id=DEP, amount_requested=40 * AUSD, already_sent=0, emergency_claimed=False
        ),
    }
    values.update(overrides)
    return TransferFacts(**values)


def agree(t: TransferInstruction, **overrides: Any) -> VerdictFacts:
    values: dict[str, Any] = {
        "verdict": "agree",
        "used": False,
        "dependent_id": DEP,
        "idempotency_key": t.idempotency_key,
        "amount": t.amount,
        "category": t.category.value,
    }
    values.update(overrides)
    return VerdictFacts(**values)


def test_happy_path_clears() -> None:
    assert check_transfer(facts()) is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"family_found": False}, "unknown dependent"),
        ({"frozen": True}, "frozen by guardian"),
        ({"policy": None}, "no active policy"),
        ({"policy_version": None}, "no active policy"),
        ({"policy_signature_valid": False}, "policy signature invalid"),
        ({"instruction": instruction(policy_version=2)}, "stale policy version"),
        ({"destination_registered": False}, "recipient not registered"),
        ({"instruction": instruction(category="gaming")}, "blocked category"),
        ({"instruction": instruction(amount=41 * AUSD)}, "over max per request"),
        ({"spent": 93 * AUSD}, "over weekly cap"),
        ({"reserved": 93 * AUSD}, "over weekly cap"),
        ({"delegated_remaining": None}, "insufficient delegation"),
        ({"delegated_remaining": 7 * AUSD}, "insufficient delegation"),
        ({"request": None}, "unknown request"),
        (
            {"request": RequestFacts("d2", 40 * AUSD, 0, False)},
            "unknown request",
        ),
        ({"request": RequestFacts(DEP, None, 0, False)}, "no amount requested"),
        ({"request": RequestFacts(DEP, 10 * AUSD, 5 * AUSD, False)}, "over requested amount"),
        (
            {"request": RequestFacts(DEP, 10 * AUSD, 0, False, already_pending=3 * AUSD)},
            "over requested amount",
        ),
        ({"instruction": instruction(amount=25 * AUSD)}, "auditor verdict required"),
        ({"proofs": [None]}, "unknown proof"),
        ({"proofs": [proof(request_id="r2")]}, "proof not for this request"),
    ],
)
def test_each_check_blocks_with_its_reason(overrides: dict[str, Any], reason: str) -> None:
    assert check_transfer(facts(**overrides)) == reason


def test_first_failure_wins() -> None:
    everything_wrong = facts(
        frozen=True,
        policy_signature_valid=False,
        destination_registered=False,
        instruction=instruction(category="gaming", amount=99 * AUSD),
        delegated_remaining=0,
    )
    assert check_transfer(everything_wrong) == "frozen by guardian"


def test_reservoir_reservations_count_against_delegation() -> None:
    """Pending money of any dependent on the same reservoir is not available to this one."""
    assert check_transfer(facts(reservoir_reserved=5 * AUSD, delegated_remaining=12 * AUSD)) == (
        "insufficient delegation"
    )
    assert check_transfer(facts(reservoir_reserved=5 * AUSD, delegated_remaining=13 * AUSD)) is None


def test_exactly_at_limits_clears() -> None:
    assert check_transfer(facts(spent=92 * AUSD)) is None
    exact = RequestFacts(DEP, 10 * AUSD, 2 * AUSD, False)
    assert check_transfer(facts(request=exact)) is None


class TestAuditorVerdict:
    """SEC-06: a transfer that needs review cannot clear without a matching, unused agree."""

    def big(self) -> TransferInstruction:
        return instruction(amount=25 * AUSD, proof_ids=["p1"], proof_findings=FINDINGS)

    def test_bound_agree_clears(self) -> None:
        t = self.big()
        assert check_transfer(facts(instruction=t, verdict=agree(t), proofs=[proof()])) is None

    @pytest.mark.parametrize(
        "change",
        [
            {"verdict": "flag"},
            {"used": True},
            {"idempotency_key": "req:r1:2"},
            {"amount": 24 * AUSD},
            {"category": "food"},
            {"dependent_id": "d2"},
        ],
    )
    def test_verdict_must_match_this_exact_transfer(self, change: dict[str, Any]) -> None:
        t = self.big()
        verdict = agree(t, **change)
        assert check_transfer(facts(instruction=t, verdict=verdict, proofs=[proof()])) == (
            "auditor verdict invalid"
        )

    def test_emergency_always_needs_review(self) -> None:
        emergency = RequestFacts(DEP, 40 * AUSD, 0, emergency_claimed=True)
        assert check_transfer(facts(request=emergency)) == "auditor verdict required"

    def test_off_pattern_amount_needs_review(self) -> None:
        past = [
            HistoryItem(
                at=datetime(2026, 10, 1, tzinfo=UTC),
                kind="transfer",
                amount=3 * AUSD,
                category="education",
            )
        ] * 3
        assert check_transfer(facts(history=past)) == "auditor verdict required"


class TestProofs:
    def needs_proof(self, **overrides: Any) -> TransferInstruction:
        values: dict[str, Any] = {
            "amount": 25 * AUSD,
            "proof_ids": ["p1"],
            "proof_findings": FINDINGS,
        }
        values.update(overrides)
        return instruction(**values)

    def check(self, t: TransferInstruction, **overrides: Any) -> str | None:
        values: dict[str, Any] = {"instruction": t, "verdict": agree(t), "proofs": [proof()]}
        values.update(overrides)
        return check_transfer(facts(**values))

    def test_usable_proof_settles_it_outright(self) -> None:
        t = self.needs_proof()
        assert self.check(t) is None
        settled = facts(instruction=t, verdict=agree(t), proofs=[proof()])
        assert not owes_proof_after(settled, POLICY)

    def test_below_threshold_needs_no_proof(self) -> None:
        assert check_transfer(facts(instruction=instruction(amount=24 * AUSD))) is None

    def test_missing_proof_sends_and_owes_a_receipt(self) -> None:
        # The dependent cannot buy anything before being paid, so the money goes out and
        # the receipt is owed afterwards (SPEC §8.9).
        t = self.needs_proof(proof_ids=[], proof_findings=None)
        assert self.check(t, proofs=[]) is None
        assert owes_proof_after(facts(instruction=t, verdict=agree(t), proofs=[]), POLICY)

    @pytest.mark.parametrize(
        "bad", [{"source": "typed"}, {"flags": ["duplicate"]}, {"media_type": "text/plain"}]
    )
    def test_proof_that_does_not_count_still_owes_a_receipt(self, bad: dict[str, Any]) -> None:
        # It no longer blocks the transfer, but a typed claim or a duplicate does not
        # settle the debt either: a real receipt is still owed.
        t = self.needs_proof()
        assert self.check(t, proofs=[proof(**bad)]) is None
        assert owes_proof_after(
            facts(instruction=t, verdict=agree(t), proofs=[proof(**bad)]), POLICY
        )

    @pytest.mark.parametrize("key", ["id:p1", "sha256:" + "a" * 64])
    def test_reused_proof_blocks(self, key: str) -> None:
        assert self.check(self.needs_proof(), used_proof_keys=frozenset({key})) == (
            "proof already used"
        )

    def test_new_photo_of_a_used_receipt_blocks(self) -> None:
        fingerprint = "receipt:bookshop|20261014|30000000|"
        assert self.check(self.needs_proof(), used_proof_keys=frozenset({fingerprint})) == (
            "proof already used"
        )

    @pytest.mark.parametrize(
        "findings",
        [
            None,
            FINDINGS.model_copy(update={"matches_claim": False}),
            FINDINGS.model_copy(update={"total": 20 * AUSD}),
        ],
    )
    def test_findings_must_cover_the_amount(self, findings: ProofFindingsRecord | None) -> None:
        t = self.needs_proof(proof_findings=findings)
        assert self.check(t) == "proof does not cover the transfer"

    def test_valid_proof_clears(self) -> None:
        assert self.check(self.needs_proof()) is None


class TestAllowance:
    def test_usual_allowance_clears_without_review(self) -> None:
        assert check_transfer(facts(instruction=allowance(), request=None)) is None

    def test_reduced_allowance_clears(self) -> None:
        assert check_transfer(facts(instruction=allowance(amount=20 * AUSD), request=None)) is None

    def test_above_allowance_blocks(self) -> None:
        assert check_transfer(facts(instruction=allowance(amount=31 * AUSD), request=None)) == (
            "over allowance"
        )

    def test_no_allowance_in_policy_blocks(self) -> None:
        no_allowance = POLICY.model_copy(update={"allowance": None})
        assert check_transfer(facts(instruction=allowance(), policy=no_allowance)) == (
            "no allowance in policy"
        )

    @pytest.mark.parametrize(
        "bad", [{"idempotency_key": "allowance:d1:2026-W43"}, {"request_id": "r1"}]
    )
    def test_one_allowance_per_week_with_the_expected_key(self, bad: dict[str, Any]) -> None:
        assert check_transfer(facts(instruction=allowance(**bad))) == "invalid allowance"

    def test_weekly_cap_still_applies(self) -> None:
        assert check_transfer(facts(instruction=allowance(), spent=71 * AUSD)) == (
            "over weekly cap"
        )


class TestGuardianOverride:
    """A signed approval of this exact amount skips the amount cap, the weekly cap and the
    Auditor requirement only (SEC-05)."""

    def approved(self, **overrides: Any) -> TransferFacts:
        findings = FINDINGS.model_copy(update={"total": 60 * AUSD})
        t = instruction(amount=60 * AUSD, proof_ids=["p1"], proof_findings=findings)
        values: dict[str, Any] = {
            "instruction": t,
            "request": RequestFacts(DEP, 60 * AUSD, 0, False),
            "spent": 50 * AUSD,
            "proofs": [proof()],
            "guardian_override": True,
        }
        values.update(overrides)
        return facts(**values)

    def test_override_clears_over_limit_request(self) -> None:
        assert check_transfer(self.approved()) is None

    def test_override_does_not_skip_an_outstanding_receipt(self) -> None:
        # A signed approval buys past the caps and the review, never past money that is
        # already out and waiting on its receipt.
        assert check_transfer(self.approved(owes_proof=True)) == "proof outstanding"

    def test_same_request_without_override_is_blocked(self) -> None:
        assert check_transfer(self.approved(guardian_override=False)) == "over max per request"

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"frozen": True}, "frozen by guardian"),
            ({"policy_signature_valid": False}, "policy signature invalid"),
            ({"destination_registered": False}, "recipient not registered"),
            (
                {"instruction": instruction(amount=60 * AUSD, category="gaming", proof_ids=["p1"])},
                "blocked category",
            ),
            ({"delegated_remaining": 59 * AUSD}, "insufficient delegation"),
            ({"request": RequestFacts(DEP, 60 * AUSD, 1, False)}, "over requested amount"),
        ],
    )
    def test_override_never_skips_hard_checks(self, overrides: dict[str, Any], reason: str) -> None:
        assert check_transfer(self.approved(**overrides)) == reason
