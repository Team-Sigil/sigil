"""The main agent (maker): a manual tool-use loop with a guardrail at every step.

Per run:

1. Read context and the request from the platform API; sanitise and delimit input.
2. Loop: call the model, validate every tool call (ToolGuard), execute allowed calls
   through the platform API. A transfer needing review goes to the Auditor first; a
   flag turns it into a guardian question and no money moves.
3. Reconcile the model's claimed outcome with what actually happened, check the reply
   to the dependent, and record the decision through the platform API.

Any failure (model unavailable, refusal, budget exhausted, loop limit, missing finish)
ends in a safe fallback: no further money moves and the guardian is asked.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_layer.auditor.auditor import Auditor, ReviewCase
from agent_layer.auditor.triggers import agent_triggers, trusted_triggers
from agent_layer.config import Limits
from agent_layer.contracts import (
    AgentContext,
    AgentEvent,
    AskForProofArgs,
    AskGuardianArgs,
    DecisionRecord,
    DependentRequest,
    FinishArgs,
    GuardianQuestion,
    Notification,
    NotifyGuardianArgs,
    Outcome,
    PolicyProposal,
    ProofFindings,
    ProofFindingsRecord,
    ProofRequest,
    ProofOwed,
    ProposePolicyUpdateArgs,
    ReportReceiptArgs,
    RequestTransferArgs,
    SettledObligation,
    SettleObligation,
    TransferInstruction,
)
from agent_layer.guardrails.budget import BudgetExhausted, TokenBudget
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.input import PlatformText
from agent_layer.i18n import dependent_locale, guardian_locale, has_text, language_instruction, language_mismatch, t
from agent_layer.guardrails.output import RunFacts, check_reply, reconcile_outcome, safe_reply, text_weight
from agent_layer.guardrails.tools import Mode, ToolGuard, ToolRejected
from agent_layer.llm import LLM, LLMUnavailable, RunDeadline
from agent_layer.maker.context import PreparedInput, context_block, prepare_request_input
from agent_layer.maker.prompts import ALLOWANCE_INSTRUCTION, MAKER_SYSTEM, RECEIPT_INSTRUCTION
from agent_layer.maker.tools import tool_definitions
from agent_layer.money import to_base, to_display
from agent_layer.platform_api import MakerAPI, PlatformAPIError

logger = logging.getLogger(__name__)

FINISH_NUDGE = "Please call the finish tool now to end this request."
# A model call needs at least this long; with less time left, the run stops.
MIN_CALL_SECONDS = 10.0


@dataclass
class _Run:
    ctx: AgentContext
    mode: Mode
    request: DependentRequest | None
    prepared: PreparedInput
    idempotency_key: str
    log: GuardrailLog
    guard: ToolGuard
    budget: TokenBudget
    deadline: float
    amount_sent: int = 0
    amount_pending: int = 0
    transfer_blocked: bool = False
    auditor_flagged: bool = False
    proof_requested: bool = False
    guardian_asked: bool = False
    # Proof after payment: the receipt this run's payment left owed, and the platform's
    # answer when a receipt run offered one.
    receipt_owed: ProofOwed | None = None
    receipt_result: SettledObligation | None = None
    finish: FinishArgs | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def request_id(self) -> str | None:
        return self.request.id if self.request else None


def _findings_record(f: ProofFindings | None) -> ProofFindingsRecord | None:
    if f is None:
        return None
    return ProofFindingsRecord(
        merchant=f.merchant, date=f.date, total=to_base(f.total), receipt_number=f.receipt_number,
        matches_claim=f.matches_claim,
    )


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class MakerAgent:
    def __init__(
        self,
        llm: LLM,
        api: MakerAPI,
        auditor: Auditor,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._llm = llm
        self._api = api
        self._auditor = auditor
        self._limits = limits or Limits()
        self._clock = clock

    def _time_left(self, run: _Run) -> float:
        """Seconds left for the next model call; raises ``RunDeadline`` when too few."""
        left = run.deadline - self._clock()
        if left < MIN_CALL_SECONDS:
            run.log.add("run_deadline", f"{self._limits.max_run_seconds:.0f}s")
            raise RunDeadline("run time budget spent")
        return left

    # ------------------------------------------------------------------ entry points

    def handle_request(self, request_id: str) -> DecisionRecord:
        request = self._api.get_request(request_id)
        ctx = self._api.get_context(request.dependent_id)
        log = GuardrailLog()
        refs = request.proofs[: self._limits.max_proofs_per_request]
        if len(request.proofs) > len(refs):
            log.add("proofs_capped", str(len(request.proofs)))
        proofs = [self._api.get_proof(ref.id) for ref in refs]
        prepared = prepare_request_input(
            ctx, request, proofs, self._limits, log, images_readable=self._llm.supports_images,
            documents_readable=self._llm.supports_documents,
        )
        owed = request.proof_owed
        if owed is not None:
            # Paid first: this run checks the receipt it owes and moves no money.
            purpose = PlatformText(log).inline(
                owed.purpose or "the earlier payment", max_chars=self._limits.max_platform_reason_chars,
                source="owed_purpose",
            )
            prepared.content.append(
                {"type": "text", "text": RECEIPT_INSTRUCTION.format(amount=to_display(owed.amount), purpose=purpose)}
            )
        run = self._new_run(
            ctx,
            "receipt" if owed is not None else "request",
            request,
            prepared,
            # One key per request *state*: re-processing the same state can never pay
            # twice, while a follow-up turn (e.g. after a receipt) gets a fresh key.
            idempotency_key=f"req:{request.id}:{len(request.thread)}",
            log=log,
        )
        return self._execute_run(run)

    def run_allowance(self, dependent_id: str, period_key: str) -> DecisionRecord:
        ctx = self._api.get_context(dependent_id)
        log = GuardrailLog()
        usual = to_display(ctx.policy.allowance.amount) if ctx.policy.allowance else "not set"
        pt = PlatformText(log)
        dep_locale, guard_locale = dependent_locale(ctx), guardian_locale(ctx)
        instruction = ALLOWANCE_INSTRUCTION.format(
            usual=usual, languages=language_instruction(dep_locale, guard_locale)
        )
        text = f"{context_block(ctx, self._limits, pt)}\n\n{instruction}"
        prepared = PreparedInput(
            content=[{"type": "text", "text": text}],
            case_text=f"Scheduled weekly allowance (usual amount {usual}).",
            signals=pt.signals,
            dependent_locale=dep_locale,
            guardian_locale=guard_locale,
        )
        run = self._new_run(
            ctx, "allowance", None, prepared, idempotency_key=f"allowance:{dependent_id}:{period_key}", log=log
        )
        return self._execute_run(run)

    # ------------------------------------------------------------------------- loop

    def _new_run(
        self,
        ctx: AgentContext,
        mode: Mode,
        request: DependentRequest | None,
        prepared: PreparedInput,
        *,
        idempotency_key: str,
        log: GuardrailLog,
    ) -> _Run:
        return _Run(
            ctx=ctx,
            mode=mode,
            request=request,
            prepared=prepared,
            idempotency_key=idempotency_key,
            log=log,
            guard=ToolGuard(ctx, self._limits, log, mode=mode, request=request, proof_ids=prepared.proof_ids),
            budget=TokenBudget(self._api, ctx.guardian_id, log),
            deadline=self._clock() + self._limits.max_run_seconds,
        )

    def _execute_run(self, run: _Run) -> DecisionRecord:
        try:
            self._loop(run)
        except (LLMUnavailable, BudgetExhausted) as exc:
            run.log.add("safe_fallback", type(exc).__name__)
            logger.warning("%s run fell back to the guardian: %s: %s", run.mode, type(exc).__name__, exc)
        except PlatformAPIError as exc:
            run.log.add("safe_fallback", f"platform error {exc.status}")
            logger.warning("%s run fell back to the guardian: %s", run.mode, exc)
        if run.finish is None:
            self._fallback_to_guardian(run)
        return self._conclude(run)

    def _loop(self, run: _Run) -> None:
        tools = tool_definitions(run.mode)
        messages: list[dict] = [{"role": "user", "content": run.prepared.content}]
        nudged = False
        for _ in range(self._limits.max_loop_turns):
            run.budget.ensure_available()
            resp = self._llm.create(
                system=MAKER_SYSTEM, messages=messages, tools=tools, timeout=self._time_left(run)
            )
            run.budget.record(resp)
            if resp.stop_reason in ("refusal", "max_tokens"):
                run.log.add("model_stopped", str(resp.stop_reason))
                return
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                if nudged:
                    run.log.add("no_finish")
                    return
                nudged = True
                messages.append({"role": "user", "content": FINISH_NUDGE})
                continue

            results: list[dict] = []
            for block in tool_uses:
                if run.finish is not None:
                    results.append(self._result(block.id, "Request already finished.", error=True))
                    continue
                try:
                    args = run.guard.admit(block.name, block.input)
                    text = self._dispatch(run, block.name, args)
                    results.append(self._result(block.id, text))
                except ToolRejected as exc:
                    results.append(self._result(block.id, str(exc), error=True))
            if run.finish is not None:
                return
            messages.append({"role": "user", "content": results})
        run.log.add("loop_limit", str(self._limits.max_loop_turns))

    @staticmethod
    def _result(tool_use_id: str, text: str, *, error: bool = False) -> dict:
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
        if error:
            block["is_error"] = True
        return block

    # ------------------------------------------------------------------------ tools

    def _dispatch(self, run: _Run, name: str, args: object) -> str:
        match name:
            case "request_transfer":
                assert isinstance(args, RequestTransferArgs)
                return self._transfer(run, args)
            case "ask_for_proof":
                assert isinstance(args, AskForProofArgs) and run.request is not None
                self._api.request_proof(
                    ProofRequest(
                        request_id=run.request.id,
                        dependent_id=run.ctx.dependent_id,
                        kind=args.kind,
                        reason=args.reason,
                    )
                )
                run.proof_requested = True
                return "Proof requested. Call finish with outcome asked_proof."
            case "ask_guardian":
                assert isinstance(args, AskGuardianArgs)
                run.guard.precheck_guardian_question()
                self._api.ask_guardian(
                    GuardianQuestion(
                        guardian_id=run.ctx.guardian_id,
                        dependent_id=run.ctx.dependent_id,
                        request_id=run.request_id,
                        question=args.question,
                        recommendation=args.recommendation,
                        recommended_amount=(
                            to_base(args.recommended_amount) if args.recommended_amount is not None else None
                        ),
                        urgency=args.urgency,
                        agent_view=args.question,
                        dedupe_key=f"ask:{run.idempotency_key}",
                    )
                )
                run.guardian_asked = True
                return "Question queued for the guardian. Call finish with outcome asked_guardian."
            case "propose_policy_update":
                assert isinstance(args, ProposePolicyUpdateArgs)
                self._api.propose_policy_update(
                    PolicyProposal(
                        guardian_id=run.ctx.guardian_id,
                        dependent_id=run.ctx.dependent_id,
                        change=args.change,
                        evidence=args.evidence,
                        dedupe_key=f"proposal:{run.ctx.dependent_id}:{_short_hash(args.change)}",
                    )
                )
                return "Proposal sent to the guardian for signing."
            case "report_receipt":
                assert isinstance(args, ReportReceiptArgs)
                return self._report_receipt(run, args)
            case "notify_guardian":
                assert isinstance(args, NotifyGuardianArgs)
                self._api.notify(
                    Notification(audience="guardian", recipient_id=run.ctx.guardian_id, message=args.message,
                                 dependent_id=run.ctx.dependent_id)
                )
                return "Note sent."
            case "finish":
                assert isinstance(args, FinishArgs)
                run.finish = args
                return "Finished."
        raise ToolRejected(f"Unknown tool {name!r}.")  # unreachable: admit() checks the allowlist

    def _report_receipt(self, run: _Run, args: ReportReceiptArgs) -> str:
        owed = run.request.proof_owed if run.request else None
        if owed is None:
            raise ToolRejected("This request doesn't owe a receipt. Call finish.")
        if run.receipt_result is not None:
            raise ToolRejected("The receipt was already checked. Call finish.")
        if not run.prepared.proof_ids:
            run.log.add("receipt_unusable")
            raise ToolRejected(
                "Nothing attached can be used as a receipt. Use ask_for_proof to ask for a clear photo of "
                "the receipt, then finish with asked_proof."
            )
        # Code picks the proof (the latest one that counts), never the model.
        proof_id = run.prepared.proof_ids[-1]
        result = self._api.settle_obligation(
            owed.obligation_id,
            SettleObligation(proof_id=proof_id, findings=_findings_record(args.proof_findings)),
        )
        run.receipt_result = result
        if result.settled:
            run.log.add("receipt_accepted", to_display(owed.amount))
            return "The platform accepted the receipt; nothing is owed for it any more. Call finish."
        reason = PlatformText(run.log).inline(
            result.reason or "not accepted", max_chars=self._limits.max_platform_reason_chars,
            source="settle_reason",
        )
        run.log.add("receipt_rejected", reason)
        return f"The platform refused the receipt: {reason}. Call finish; the dependent will be told."

    def _transfer(self, run: _Run, args: RequestTransferArgs) -> str:
        amount = run.guard.precheck_transfer(args)
        run.guard.record_transfer_attempt()
        override = run.guard.guardian_override(amount)
        triggers = [] if override else trusted_triggers(
            amount=amount,
            category=args.category,
            policy=run.ctx.policy,
            history=run.ctx.history,
            trigger="allowance" if run.mode == "allowance" else "request",
            emergency_claimed=bool(run.request and run.request.emergency_claimed),
            proof_attached=bool(run.prepared.proof_ids),
        ) + agent_triggers(
            category_confidence=args.category_confidence,
            injection_signals=run.prepared.signals,
            proof_concerns=run.prepared.proof_concerns,
        )

        if override:
            # Exactly the amount the guardian signed for: the engine skips review too.
            run.log.add("guardian_override", to_display(amount))
        verdict_id: str | None = None
        if triggers:
            review = self._auditor.review(
                ReviewCase(
                    ctx=run.ctx,
                    request_id=run.request_id,
                    case_text=run.prepared.case_text,
                    proof_texts=run.prepared.proof_texts,
                    proof_images=run.prepared.proof_images,
                    proof_documents=run.prepared.proof_documents,
                    amount=amount,
                    category=args.category,
                    purpose=args.purpose,
                    maker_reasoning=args.reasoning,
                    category_confidence=args.category_confidence,
                    idempotency_key=run.idempotency_key,
                    triggers=triggers,
                ),
                run.budget,
                run.log,
                time_left=lambda: self._time_left(run),
            )
            if not review.agreed:
                run.auditor_flagged = True
                run.log.add("auditor_flagged", ",".join(triggers))
                self._api.ask_guardian(
                    GuardianQuestion(
                        guardian_id=run.ctx.guardian_id,
                        dependent_id=run.ctx.dependent_id,
                        request_id=run.request_id,
                        question=t(
                            "guardian.flagged", run.prepared.guardian_locale,
                            amount=to_display(amount), name=run.ctx.dependent_name, purpose=args.purpose,
                        ),
                        recommendation="approve",
                        recommended_amount=amount,
                        urgency="normal",
                        agent_view=args.reasoning,
                        auditor_view=review.verdict.reason,
                        dedupe_key=f"flag:{run.idempotency_key}",
                    )
                )
                run.guardian_asked = True
                return (
                    "Transfer held: the independent Auditor flagged it, and the guardian has been asked "
                    "to decide with both views. No money was sent. Call finish and tell the dependent "
                    "their guardian will decide."
                )
            verdict_id = review.verdict_id

        result = self._api.submit_transfer(
            TransferInstruction(
                dependent_id=run.ctx.dependent_id,
                request_id=run.request_id,
                trigger="allowance" if run.mode == "allowance" else "request",
                amount=amount,
                purpose=args.purpose,
                category=args.category,
                category_confidence=args.category_confidence,
                reasoning=args.reasoning,
                idempotency_key=run.idempotency_key,
                policy_version=run.ctx.policy.version,
                auditor_verdict_id=verdict_id,
                proof_ids=list(run.prepared.proof_ids),
                proof_findings=_findings_record(args.proof_findings),
            )
        )
        if result.status in ("sent", "duplicate"):
            run.amount_sent += result.amount
            if result.status == "duplicate":
                run.log.add("idempotent_replay", run.idempotency_key)
            if result.proof_owed is not None:
                run.receipt_owed = result.proof_owed
                run.log.add("receipt_owed", to_display(result.proof_owed.amount))
                return (
                    f"Sent {to_display(result.amount)}. No proof covered it, so the dependent now owes a "
                    "receipt for it; the app adds a line asking for it. Call finish."
                )
            return f"Sent {to_display(result.amount)}. Call finish."
        if result.status == "pending":
            run.amount_pending += result.amount or amount
            run.log.add("transfer_pending", to_display(result.amount or amount))
            return (
                f"The platform accepted {to_display(result.amount or amount)}, but the payment isn't "
                "confirmed yet and may still go out. Don't send anything else. Call finish and tell "
                "the dependent it's on its way."
            )
        run.transfer_blocked = True
        reason = PlatformText(run.log).inline(
            result.reason or "outside the limits",
            max_chars=self._limits.max_platform_reason_chars,
            source="engine_reason",
        )
        run.log.add("engine_blocked", reason)
        raise ToolRejected(
            f"The platform blocked this transfer: {reason}. "
            "Explain this to the dependent and offer an alternative."
        )

    # -------------------------------------------------------------------- conclude

    def _fallback_to_guardian(self, run: _Run) -> None:
        if run.mode != "request" or run.guardian_asked or run.amount_sent > 0 or run.amount_pending > 0:
            return
        try:
            self._api.ask_guardian(
                GuardianQuestion(
                    guardian_id=run.ctx.guardian_id,
                    dependent_id=run.ctx.dependent_id,
                    request_id=run.request_id,
                    question=t("guardian.fallback", run.prepared.guardian_locale, name=run.ctx.dependent_name),
                    recommendation="unsure",
                    agent_view=t("guardian.fallback_view", run.prepared.guardian_locale),
                    dedupe_key=f"fallback:{run.idempotency_key}",
                )
            )
            run.guardian_asked = True
        except PlatformAPIError:
            run.log.add("fallback_question_failed")

    def _conclude(self, run: _Run) -> DecisionRecord:
        facts = RunFacts(
            amount_sent=run.amount_sent,
            transfer_blocked=run.transfer_blocked,
            auditor_flagged=run.auditor_flagged,
            proof_requested=run.proof_requested,
            guardian_asked=run.guardian_asked,
            requested_amount=run.request.amount_requested if run.request else None,
            amount_pending=run.amount_pending,
        )
        dep_locale, guard_locale = run.prepared.dependent_locale, run.prepared.guardian_locale
        if run.receipt_result is not None:
            return self._record(run, *self._receipt_texts(run))
        shown = run.amount_sent or run.amount_pending  # the amount fixed replies mention
        if run.finish is None:
            claimed = Outcome.SAFE_FALLBACK
            # A receipt run asks nobody: the dependent just sends the receipt again.
            reply = (
                t("reply.receipt_unchecked", dep_locale) if run.mode == "receipt"
                else safe_reply(Outcome.SAFE_FALLBACK, locale=dep_locale)
            )
            summary = t("guardian.stopped_summary", guard_locale)
        else:
            claimed = Outcome(run.finish.outcome)
            reply, summary = run.finish.reply_to_dependent, run.finish.summary_for_guardian
        if claimed is not Outcome.SAFE_FALLBACK:
            outcome = reconcile_outcome(claimed, facts, run.log)
        elif run.auditor_flagged or run.amount_sent > 0 or run.amount_pending > 0:
            # Stopped early, but something already happened: report that, not a fallback.
            outcome = reconcile_outcome(Outcome.SENT_FULL, facts, GuardrailLog())
        else:
            outcome = claimed
        if outcome is not claimed:
            reply = safe_reply(outcome, shown, dep_locale)
        elif outcome is Outcome.OUT_OF_SCOPE and text_weight(reply) > self._limits.max_out_of_scope_reply_chars:
            # A long off-topic reply means the model answered the question: not our job.
            run.log.add("out_of_scope_reply_replaced", str(len(reply)))
            reply = safe_reply(outcome, locale=dep_locale)
        reply = check_reply(
            reply,
            outcome=outcome,
            amount_sent=shown,
            guidance=run.ctx.policy.guidance,
            max_chars=self._limits.max_reply_chars,
            log=run.log,
            locale=dep_locale,
        )
        if language_mismatch(reply, dep_locale):
            # The model ignored the language or script it was told to use.
            run.log.add("reply_language_corrected", dep_locale)
            reply = safe_reply(outcome, shown, dep_locale)
        summary = check_reply(
            summary, outcome=outcome, amount_sent=shown, guidance="", max_chars=300, log=run.log,
            locale=guard_locale,
        )
        if run.receipt_owed is not None and outcome in (Outcome.SENT_FULL, Outcome.SENT_PARTIAL):
            # Paid first: the dependent must hear that a receipt is now owed, whatever the
            # model wrote.
            reply = f"{reply} {t('reply.receipt_owed', dep_locale, amount=to_display(run.receipt_owed.amount))}"
        return self._record(run, outcome, reply, summary)

    def _receipt_texts(self, run: _Run) -> tuple[Outcome, str, str]:
        """Fixed texts for a receipt run: the platform's answer decides, never the model."""
        assert run.receipt_result is not None and run.request is not None
        owed = run.request.proof_owed
        assert owed is not None
        dep_locale, guard_locale = run.prepared.dependent_locale, run.prepared.guardian_locale
        amount = to_display(owed.amount)
        pt = PlatformText(GuardrailLog())
        name = pt.inline(run.ctx.dependent_name, max_chars=self._limits.max_name_chars, source="dependent_name")
        purpose = pt.inline(owed.purpose or "-", max_chars=self._limits.max_platform_reason_chars, source="owed_purpose")
        if run.receipt_result.settled:
            return (
                Outcome.RECEIPT_ACCEPTED,
                t("reply.receipt_accepted", dep_locale, amount=amount),
                t("guardian.receipt_accepted", guard_locale, name=name, amount=amount, purpose=purpose),
            )
        key = f"receipt.reason.{run.receipt_result.reason}"
        reason_key = key if has_text(key) else "receipt.reason.proof does not cover the amount"
        reply = f"{t(reason_key, dep_locale)} {t('reply.receipt_rejected', dep_locale, amount=amount)}"
        summary = t(
            "guardian.receipt_rejected", guard_locale, name=name, amount=amount, purpose=purpose,
            reason=t(reason_key, guard_locale),
        )
        return Outcome.RECEIPT_REJECTED, reply, summary

    def _record(self, run: _Run, outcome: Outcome, reply: str, summary: str) -> DecisionRecord:
        record = DecisionRecord(
            dependent_id=run.ctx.dependent_id,
            request_id=run.request_id,
            outcome=outcome,
            reply_to_dependent=reply,
            summary_for_guardian=summary,
            amount_sent=run.amount_sent,
            policy_version=run.ctx.policy.version,
            guardrail_events=run.log.events,
        )
        self._api.record_decision(record)
        if run.log.events:
            try:
                self._api.log_event(
                    AgentEvent(kind="guardrails", subject_id=run.request_id or run.idempotency_key,
                               detail={"events": run.log.events})
                )
            except PlatformAPIError:
                pass
        return record
