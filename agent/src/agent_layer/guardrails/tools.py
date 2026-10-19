"""Tool guardrails: every tool call is validated and pre-checked before it is executed.

A rejected call never reaches the platform API; the reason is returned to the model as
an error ``tool_result`` so it can choose another move (smaller amount, ask for proof,
ask the guardian). These pre-checks duplicate a subset of the engine's limits on
purpose: the engine stays authoritative, the agent just fails earlier and explains.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from agent_layer.config import Limits
from agent_layer.contracts import (
    AgentContext,
    AskForProofArgs,
    AskGuardianArgs,
    Category,
    DependentRequest,
    FinishArgs,
    NotifyGuardianArgs,
    ProposePolicyUpdateArgs,
    ReportReceiptArgs,
    RequestTransferArgs,
)
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.money import to_base, to_display

# "receipt": a request that was paid first and owes a receipt; the run checks the one sent.
Mode = Literal["request", "allowance", "receipt"]

TOOL_ARGS: dict[str, type[BaseModel]] = {
    "request_transfer": RequestTransferArgs,
    "ask_for_proof": AskForProofArgs,
    "ask_guardian": AskGuardianArgs,
    "propose_policy_update": ProposePolicyUpdateArgs,
    "notify_guardian": NotifyGuardianArgs,
    "report_receipt": ReportReceiptArgs,
    "finish": FinishArgs,
}

TOOLS_BY_MODE: dict[Mode, frozenset[str]] = {
    "request": frozenset(TOOL_ARGS) - {"report_receipt"},
    # A scheduled allowance has no dependent waiting on proof.
    "allowance": frozenset({"request_transfer", "ask_guardian", "notify_guardian", "finish"}),
    # Checking an owed receipt moves no money: report what it says, or ask for a better one.
    "receipt": frozenset({"report_receipt", "ask_for_proof", "notify_guardian", "finish"}),
}

# An allowance may be lowered but never raised: the engine caps it at the signed amount,
# so raising it needs the guardian.
MAX_ALLOWANCE_FACTOR = 1.0


class ToolRejected(Exception):
    """Raised with a message that is safe to show the model."""


@dataclass
class _Counters:
    calls: int = 0
    transfers: int = 0
    guardian_questions: int = 0


class ToolGuard:
    def __init__(
        self,
        ctx: AgentContext,
        limits: Limits,
        log: GuardrailLog,
        *,
        mode: Mode,
        request: DependentRequest | None = None,
        proof_ids: Sequence[str] = (),
    ):
        self.ctx = ctx
        self.proof_ids = tuple(proof_ids)  # proofs that passed the integrity checks
        self.limits = limits
        self.log = log
        self.mode = mode
        self.request = request
        self.n = _Counters()

    # ---- generic checks
    def admit(self, name: str, raw_input: object) -> BaseModel:
        """Check the call is allowed now and its arguments are valid."""
        self.n.calls += 1
        if self.n.calls > self.limits.max_tool_calls_per_run:
            self.log.add("tool_call_limit", name)
            raise ToolRejected("Tool call limit for this request reached. Call finish now.")
        if name not in TOOLS_BY_MODE[self.mode]:
            self.log.add("tool_not_allowed", name)
            raise ToolRejected(f"Tool {name!r} is not available here.")
        if not isinstance(raw_input, dict):
            self.log.add("tool_args_invalid", name)
            raise ToolRejected("Tool input must be a JSON object.")
        try:
            return TOOL_ARGS[name].model_validate(raw_input)
        except ValidationError as exc:
            self.log.add("tool_args_invalid", name)
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            raise ToolRejected(f"Invalid arguments for {name}: {problems}") from None

    # ---- tool-specific pre-checks
    def precheck_transfer(self, args: RequestTransferArgs) -> int:
        """Return the amount in base units, or raise ToolRejected with the reason."""
        ctx, policy = self.ctx, self.ctx.policy
        amount = to_base(args.amount)

        def reject(code: str, msg: str) -> ToolRejected:
            self.log.add(code, f"{to_display(amount)} {args.category}")
            return ToolRejected(msg)

        if ctx.frozen:
            raise reject("frozen", "The guardian has frozen all transfers. Do not send money; explain this kindly.")
        if self.n.transfers >= self.limits.max_transfers_per_run:
            raise reject("transfer_limit", "Only one transfer is allowed per request. Call finish now.")
        if args.category in policy.blocked_categories:
            raise reject("blocked_category", f"The guardian has blocked the {args.category} category.")
        if self.mode == "allowance":
            if args.category is not Category.ALLOWANCE:
                raise reject("wrong_category", "A scheduled allowance must use the allowance category.")
            if policy.allowance is None:
                raise reject("no_allowance_rule", "The policy has no regular allowance.")
            ceiling = int(policy.allowance.amount * MAX_ALLOWANCE_FACTOR)
            if amount > ceiling:
                raise reject(
                    "allowance_ceiling",
                    f"The allowance can't be more than the signed {to_display(ceiling)}. Send that or less, "
                    "and ask the guardian if you think it should be raised.",
                )
        elif args.category is Category.ALLOWANCE:
            raise reject("wrong_category", "The allowance category is only for the scheduled allowance.")
        override = self.guardian_override(amount)
        if not override and amount > policy.max_per_request:
            raise reject(
                "over_max_per_request",
                f"Above the guardian's per-request maximum of {to_display(policy.max_per_request)}. "
                "Offer a smaller amount or ask the guardian.",
            )
        if not override and amount > ctx.budget.remaining_this_week:
            raise reject(
                "over_weekly_budget",
                f"Only {to_display(ctx.budget.remaining_this_week)} is left this week.",
            )
        if amount > ctx.delegated_amount:
            raise reject("over_reservoir", "The reservoir does not have enough funds; ask the guardian for a top-up.")
        if self.request is not None:
            owed = ctx.proofs_owed
            if len(owed) >= ctx.max_proofs_owed:
                listed = "; ".join(f"{to_display(o.amount)} for {o.purpose or 'an earlier request'}" for o in owed)
                raise reject(
                    "proof_outstanding",
                    f"{len(owed)} receipts are still owed for money already sent ({listed}). No more money "
                    "until the dependent sends one of them. Finish with declined and ask them to send an "
                    "owed receipt in the request it belongs to.",
                )
            asked = self.request.amount_requested
            # The amount must come from the app's structured field, never from the model's
            # reading of the chat: an unclear or off-topic message can't move money.
            if asked is None:
                raise reject(
                    "no_amount",
                    "The dependent hasn't stated an amount, so no money can be sent. Ask them what it is "
                    "for and how much (finish with asked_dependent), or finish with out_of_scope if the "
                    "message isn't about money.",
                )
            # Money that is pending (accepted, not yet confirmed) may still go out: count it.
            committed = self.request.already_sent + self.request.already_pending
            if committed + amount > asked:
                raise reject(
                    "over_requested",
                    f"You may not send more than was asked for ({to_display(asked)} total, "
                    f"{to_display(self.request.already_sent)} already sent"
                    + (f", {to_display(self.request.already_pending)} pending" if self.request.already_pending else "")
                    + ").",
                )
            if amount >= policy.proof_threshold and not self.proof_ids:
                # Proof after payment: nobody can show a receipt before they've been paid. Code
                # adds an Auditor review, and the platform records the receipt as owed.
                self.log.add("pay_first", to_display(amount))
            elif amount >= policy.proof_threshold:
                f = args.proof_findings
                if f is None:
                    raise reject("proof_findings_required", "Fill proof_findings from what the proof itself says.")
                if not f.matches_claim:
                    raise reject(
                        "proof_mismatch",
                        "The proof doesn't match the request, so don't send. Ask for the right receipt or decline.",
                    )
                if amount > to_base(f.total):
                    raise reject(
                        "proof_total_too_low",
                        f"The proof only covers {to_display(to_base(f.total))}. Send at most that, or ask for proof.",
                    )
        return amount

    def guardian_override(self, amount: int) -> bool:
        """The guardian signed an approval for exactly this amount on this request.

        Like the engine's override, it lifts only the per-request max, the weekly cap and the
        Auditor review; freeze, blocked categories, the reservoir, the requested amount and
        proof still apply. The engine re-verifies the signature, so a wrong field can't move
        money it wouldn't."""
        if self.mode != "request" or self.request is None:
            return False
        return self.request.guardian_approved_amount == amount

    def record_transfer_attempt(self) -> None:
        self.n.transfers += 1

    def precheck_guardian_question(self) -> None:
        if self.n.guardian_questions >= self.limits.max_guardian_questions_per_run:
            self.log.add("guardian_question_limit")
            raise ToolRejected("You have already asked the guardian about this. Call finish now.")
        self.n.guardian_questions += 1
