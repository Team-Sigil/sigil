"""Tool guardrails: every tool call is validated and pre-checked before it is executed.

A rejected call never reaches the platform API; the reason is returned to the model as
an error ``tool_result`` so it can choose another move (smaller amount, ask for proof,
ask the guardian). These pre-checks duplicate a subset of the engine's limits on
purpose: the engine stays authoritative, the agent just fails earlier and explains.
"""

from __future__ import annotations

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
    RequestTransferArgs,
)
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.money import to_base, to_display

Mode = Literal["request", "allowance"]

TOOL_ARGS: dict[str, type[BaseModel]] = {
    "request_transfer": RequestTransferArgs,
    "ask_for_proof": AskForProofArgs,
    "ask_guardian": AskGuardianArgs,
    "propose_policy_update": ProposePolicyUpdateArgs,
    "notify_guardian": NotifyGuardianArgs,
    "finish": FinishArgs,
}

TOOLS_BY_MODE: dict[Mode, frozenset[str]] = {
    "request": frozenset(TOOL_ARGS),
    # A scheduled allowance has no dependent waiting on proof.
    "allowance": frozenset({"request_transfer", "ask_guardian", "notify_guardian", "finish"}),
}

# An adjusted allowance may not exceed the usual amount by more than this factor.
MAX_ALLOWANCE_FACTOR = 1.5


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
    ):
        self.ctx = ctx
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
                    f"An adjusted allowance may be at most {to_display(ceiling)}. Ask the guardian for more.",
                )
        elif args.category is Category.ALLOWANCE:
            raise reject("wrong_category", "The allowance category is only for the scheduled allowance.")
        if amount > policy.max_per_request:
            raise reject(
                "over_max_per_request",
                f"Above the guardian's per-request maximum of {to_display(policy.max_per_request)}. "
                "Offer a smaller amount or ask the guardian.",
            )
        if amount > ctx.budget.remaining_this_week:
            raise reject(
                "over_weekly_budget",
                f"Only {to_display(ctx.budget.remaining_this_week)} is left this week.",
            )
        if amount > ctx.delegated_amount:
            raise reject("over_reservoir", "The reservoir does not have enough funds; ask the guardian for a top-up.")
        if self.request is not None:
            asked = self.request.amount_requested
            if asked is not None and self.request.already_sent + amount > asked:
                raise reject(
                    "over_requested",
                    f"You may not send more than was asked for ({to_display(asked)} total, "
                    f"{to_display(self.request.already_sent)} already sent).",
                )
            if amount >= policy.proof_threshold and not self.request.proofs:
                raise reject(
                    "proof_required",
                    f"Amounts of {to_display(policy.proof_threshold)} or more need proof. Use ask_for_proof first.",
                )
        return amount

    def record_transfer_attempt(self) -> None:
        self.n.transfers += 1

    def precheck_guardian_question(self) -> None:
        if self.n.guardian_questions >= self.limits.max_guardian_questions_per_run:
            self.log.add("guardian_question_limit")
            raise ToolRejected("You have already asked the guardian about this. Call finish now.")
        self.n.guardian_questions += 1
