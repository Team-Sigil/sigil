"""The policy engine's decision: pure, deterministic, deny by default.

`check_transfer` takes every fact the decision needs, already loaded and verified by
`engine/transfers.py`, and returns `None` (cleared) or the reason the transfer is blocked.
No database, network, clock or randomness here, so every rule is unit-testable.
"""

from dataclasses import dataclass, field

from gateway.contracts.agent import HistoryItem, Proof, TransferInstruction
from gateway.contracts.policy import PolicyBody, policy_view
from gateway.engine.proofs import counts_as_proof, proof_keys
from gateway.engine.triggers import trusted_triggers


@dataclass(frozen=True)
class RequestFacts:
    dependent_id: str
    amount_requested: int | None
    already_sent: int
    emergency_claimed: bool
    # Cleared but not yet confirmed: it may still go out, so it counts as sent here.
    already_pending: int = 0


@dataclass(frozen=True)
class VerdictFacts:
    verdict: str
    used: bool
    dependent_id: str
    idempotency_key: str
    amount: int
    category: str


@dataclass(frozen=True)
class TransferFacts:
    """`None` or `False` always means "missing or unverified" and blocks."""

    instruction: TransferInstruction
    family_found: bool
    frozen: bool
    policy: PolicyBody | None
    policy_version: int | None
    policy_signature_valid: bool
    destination_registered: bool
    spent: int
    reserved: int  # this family's pending money this week (weekly cap)
    # Pending money of every family drawing on the same reservoir: a guardian's dependents
    # share one token account and one delegation.
    reservoir_reserved: int
    delegated_remaining: int | None  # 0 or None unless the delegate is the agent wallet
    expected_allowance_key: str
    request: RequestFacts | None = None
    history: list[HistoryItem] = field(default_factory=list)
    verdict: VerdictFacts | None = None
    proofs: list[Proof | None] = field(default_factory=list)
    used_proof_keys: frozenset[str] = frozenset()
    # Verified-capture mode (CAMERA_ONLY_PROOFS), and the photos OCR proofs were read from.
    camera_only: bool = False
    proof_origins: dict[str, Proof] = field(default_factory=dict)
    # The same receipt already backed a transfer in any family (keyed fingerprint).
    receipt_already_used: bool = False
    # Set only when a wallet-signed guardian approval of exactly this amount for this request
    # was found and verified (SPEC SEC-05). Lets the transfer past the amount cap, the weekly
    # cap and the Auditor requirement; never past anything else.
    guardian_override: bool = False
    # An earlier transfer at or above the proof threshold whose receipt has not arrived yet.
    # The dependent gets no more money until it is settled (SPEC §8.9).
    owes_proof: bool = False


def check_transfer(facts: TransferFacts) -> str | None:
    """Runs the checks in a fixed order and returns the first failure, or None if cleared."""
    t = facts.instruction
    override = facts.guardian_override

    if not facts.family_found:
        return "unknown dependent"
    if facts.frozen:
        return "frozen by guardian"
    if facts.policy is None or facts.policy_version is None:
        return "no active policy"
    if not facts.policy_signature_valid:
        return "policy signature invalid"
    if t.policy_version != facts.policy_version:
        return "stale policy version"
    if not facts.destination_registered:
        return "recipient not registered"
    # Money already sent is waiting on its receipt. The scheduled allowance is the
    # guardian's own standing instruction, so it is not held back by this.
    if facts.owes_proof and t.trigger == "request":
        return "proof outstanding"
    policy = facts.policy

    if t.category in policy.blocked_categories:
        return "blocked category"

    # Amount cap: the allowance is capped by its own signed amount, requests by the
    # per-request maximum. Raising either needs the guardian.
    if t.trigger == "allowance":
        if policy.allowance is None:
            return "no allowance in policy"
        if t.request_id is not None or t.idempotency_key != facts.expected_allowance_key:
            return "invalid allowance"
        if t.amount > policy.allowance.amount:
            return "over allowance"
    elif not override and t.amount > policy.max_per_request:
        return "over max per request"

    if not override and facts.spent + facts.reserved + t.amount > policy.weekly_cap:
        return "over weekly cap"
    if (
        facts.delegated_remaining is None
        or t.amount > facts.delegated_remaining - facts.reservoir_reserved
    ):
        return "insufficient delegation"

    request = facts.request
    if t.trigger == "request":
        if request is None or request.dependent_id != t.dependent_id:
            return "unknown request"
        if request.amount_requested is None:
            return "no amount requested"
        still_open = request.amount_requested - request.already_sent - request.already_pending
        if t.amount > still_open:
            return "over requested amount"

    if not override:
        reason = _review_failure(facts, policy)
        if reason:
            return reason

    return _proof_failure(facts, policy)


def _review_failure(facts: TransferFacts, policy: PolicyBody) -> str | None:
    t = facts.instruction
    needs_review = trusted_triggers(
        amount=t.amount,
        category=t.category,
        policy=policy_view(policy, version=facts.policy_version or 0, body_hash=""),
        history=facts.history,
        trigger=t.trigger,
        emergency_claimed=bool(facts.request and facts.request.emergency_claimed),
    )
    if not needs_review:
        return None
    v = facts.verdict
    if v is None:
        return "auditor verdict required"
    if (
        v.used
        or v.verdict != "agree"
        or v.idempotency_key != t.idempotency_key
        or v.amount != t.amount
        or v.category != t.category
        or v.dependent_id != t.dependent_id
    ):
        return "auditor verdict invalid"
    return None


def _proof_failure(facts: TransferFacts, policy: PolicyBody) -> str | None:
    """The engine's own proof records decide, not the agent's claims.

    Proof no longer has to exist before the money moves: a dependent cannot buy anything
    without being paid first, so a transfer at or above `proof_threshold` goes out and
    leaves an obligation behind (`owes_proof`). What this still enforces is that any proof
    the agent *does* attach is real, belongs to the request, and has not already backed a
    transfer — so a receipt can never be spent twice.
    """
    t = facts.instruction
    if any(p is None for p in facts.proofs):
        return "unknown proof"
    proofs = [p for p in facts.proofs if p is not None]
    if any(p.request_id != t.request_id or p.dependent_id != t.dependent_id for p in proofs):
        return "proof not for this request"
    if t.trigger != "request" or not proofs:
        return None

    def counts(p: Proof) -> bool:
        origin = facts.proof_origins.get(p.derived_from) if p.derived_from else None
        return counts_as_proof(p, camera_only=facts.camera_only, origin=origin)

    usable = [
        p
        for p in proofs
        if counts(p) and not set(proof_keys(p, t.proof_findings)) & facts.used_proof_keys
    ]
    if not usable:
        # Nothing attached that counts: the transfer still goes out and the receipt is owed.
        return "proof already used" if any(counts(p) for p in proofs) else None
    findings = t.proof_findings
    if findings is None or not findings.matches_claim or findings.total < t.amount:
        return "proof does not cover the transfer"
    if facts.receipt_already_used:
        return "proof already used"
    return None


def owes_proof_after(facts: TransferFacts, policy: PolicyBody) -> bool:
    """Whether this cleared transfer leaves a receipt owed.

    Owed for a request at or above the threshold, unless a proof that *counts* as evidence
    already covered it. Typed text is the dependent's claim, not evidence, so it never
    settles the debt.
    """
    t = facts.instruction
    if t.trigger != "request" or t.amount < policy.proof_threshold:
        return False
    findings = t.proof_findings
    if findings is None or not findings.matches_claim or findings.total < t.amount:
        return True
    return not any(
        counts_as_proof(
            p,
            camera_only=facts.camera_only,
            origin=facts.proof_origins.get(p.derived_from) if p.derived_from else None,
        )
        for p in facts.proofs
        if p is not None
    )
