"""Proof after payment: money above the proof threshold goes out first (after an Auditor
review), the receipt is owed, and a later run checks it. The platform, not the model,
decides whether a receipt settles what is owed; at three owed receipts no more money goes
out for requests."""

from __future__ import annotations

import base64

from agent_layer.contracts import Outcome, Proof, ProofRef
from agent_layer.money import to_base
from tests.conftest import AGREE
from tests.fakes import all_text, findings, finish, reply, tool_use, transfer

T = to_base
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-pixels" * 20


def _image(pid: str, data: bytes = PNG) -> Proof:
    return Proof(id=pid, media_type="image/png", size_bytes=len(data), data_b64=base64.b64encode(data).decode())


def _pay_first(platform, make_agent, add_request, rid: str = "r1", amount: float = 30):
    add_request(rid, amount=amount, message="I need a calculator for maths class")
    agent, maker, auditor = make_agent(
        [reply(transfer(amount, purpose="calculator")), reply(finish("sent_full", "Sent, good luck in maths!"))],
        AGREE,
    )
    return agent.handle_request(rid), maker, auditor


def _upload(platform, rid: str, pid: str, data: bytes = PNG) -> None:
    platform.add_proof(_image(pid, data))
    request = platform.requests[rid]
    platform.requests[rid] = request.model_copy(
        update={"proofs": [*request.proofs, ProofRef(id=pid, media_type="image/png")]}
    )


def test_money_goes_out_first_after_a_review_and_the_receipt_is_owed(platform, make_agent, add_request):
    record, _, auditor = _pay_first(platform, make_agent, add_request)

    assert record.outcome is Outcome.SENT_FULL and record.amount_sent == T(30)
    assert platform.sent[0].proof_ids == []
    verdict = next(iter(platform.verdicts.values())).submission
    assert "pay_first" in verdict.review_triggers and auditor.calls
    owed = platform.open_obligations("d1")
    assert [(o.owed.amount, o.owed.purpose) for o in owed] == [(T(30), "calculator")]
    # Whatever the model wrote, the dependent is told a receipt is owed.
    assert record.reply_to_dependent.startswith("Sent, good luck in maths!")
    assert "send a photo of the receipt for 30.00" in record.reply_to_dependent


def test_a_flagged_pay_first_request_sends_nothing(platform, make_agent, add_request):
    from tests.conftest import FLAG

    add_request(amount=30)
    agent, *_ = make_agent([reply(transfer(30)), reply(finish("sent_full", "Sent!"))], FLAG)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.AUDITOR_FLAGGED and platform.sent == []
    assert platform.open_obligations("d1") == []


def test_below_the_threshold_nothing_is_owed(platform, make_agent, add_request):
    add_request(amount=10)
    agent, *_ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL and platform.open_obligations("d1") == []
    assert "receipt" not in record.reply_to_dependent


# ------------------------------------------------------------------ receipt runs


def test_the_owed_receipt_is_checked_and_accepted(platform, make_agent, add_request):
    _pay_first(platform, make_agent, add_request)
    _upload(platform, "r1", "p1")
    agent, maker, _ = make_agent([
        reply(tool_use("report_receipt", proof_findings=findings(30))),
        reply(finish("sent_full", "Thanks!")),  # the model's claim doesn't decide the outcome
    ])
    record = agent.handle_request("r1")

    assert record.outcome is Outcome.RECEIPT_ACCEPTED
    assert record.reply_to_dependent == "Thanks! Your receipt for 30.00 is checked, so you're all set."
    assert "sent the receipt for 30.00 (calculator)" in record.summary_for_guardian
    assert platform.open_obligations("d1") == [] and len(platform.sent) == 1
    shown = all_text(maker.calls[0]["messages"])
    assert "owes a receipt for 30.00" in shown and "calculator" in shown
    assert {t["name"] for t in maker.calls[0]["tools"]} == {"report_receipt", "ask_for_proof", "notify_guardian",
                                                            "finish"}


def test_a_receipt_that_does_not_cover_it_is_refused_by_the_platform(platform, make_agent, add_request):
    _pay_first(platform, make_agent, add_request)
    _upload(platform, "r1", "p1")
    agent, *_ = make_agent([
        reply(tool_use("report_receipt", proof_findings=findings(12))),
        reply(finish("asked_proof", "Hmm.")),
    ])
    record = agent.handle_request("r1")

    assert record.outcome is Outcome.RECEIPT_REJECTED
    assert record.reply_to_dependent.startswith("It doesn't show the purchase or the full amount.")
    assert "clear photo of the receipt for 30.00" in record.reply_to_dependent
    assert "receipt_rejected: proof does not cover the amount" in record.guardrail_events
    assert len(platform.open_obligations("d1")) == 1


def test_a_receipt_that_already_backed_a_payment_cannot_settle(platform, make_agent, add_request):
    # r0 was paid with this receipt attached; the same photo can't settle r1's debt.
    add_request("r0", amount=30, proofs=[ProofRef(id="p0", media_type="image/png")])
    platform.add_proof(_image("p0"))
    agent, *_ = make_agent([reply(transfer(30, proof=findings(30))), reply(finish("sent_full", "Sent!"))], AGREE)
    agent.handle_request("r0")
    _pay_first(platform, make_agent, add_request)
    _upload(platform, "r1", "p1")  # same bytes as p0
    agent, *_ = make_agent([reply(tool_use("report_receipt", proof_findings=findings(30))),
                            reply(finish("asked_proof", "?"))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.RECEIPT_REJECTED
    assert "already used for another payment" in record.reply_to_dependent


def test_without_a_usable_receipt_the_agent_asks_for_one(platform, make_agent, add_request):
    _pay_first(platform, make_agent, add_request)
    platform.add_proof(Proof(id="t1", media_type="text/plain", size_bytes=10, text="paid 30", source="typed"))
    platform.requests["r1"] = platform.requests["r1"].model_copy(
        update={"proofs": [ProofRef(id="t1", media_type="text/plain")]}
    )
    agent, *_ = make_agent([
        reply(tool_use("report_receipt", proof_findings=findings(30))),  # refused: nothing counts
        reply(tool_use("ask_for_proof", kind="receipt", reason="A photo of the receipt, please.")),
        reply(finish("asked_proof", "Could you send a photo of the receipt?")),
    ])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.ASKED_PROOF
    assert "receipt_unusable" in record.guardrail_events
    assert len(platform.open_obligations("d1")) == 1


def test_a_receipt_run_cannot_send_money(platform, make_agent, add_request):
    _pay_first(platform, make_agent, add_request)
    agent, *_ = make_agent([reply(transfer(5)), reply(finish("declined", "No."))])
    record = agent.handle_request("r1")
    assert "tool_not_allowed: request_transfer" in record.guardrail_events and len(platform.sent) == 1


def test_a_model_outage_in_a_receipt_run_asks_nobody(platform, make_agent, add_request):
    from tests.fakes import unavailable

    _pay_first(platform, make_agent, add_request)
    _upload(platform, "r1", "p1")
    agent, *_ = make_agent([unavailable()])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SAFE_FALLBACK
    assert record.reply_to_dependent.startswith("I couldn't check your receipt just now")
    assert platform.questions == {}


# ------------------------------------------------------------------- the limit


def test_up_to_three_receipts_may_be_owed(platform, make_agent, add_request):
    platform.dependents["d1"].spent_this_week = 0
    for rid in ("r1", "r2", "r3"):
        record, *_ = _pay_first(platform, make_agent, add_request, rid=rid, amount=25)
        assert record.outcome is Outcome.SENT_FULL
    assert len(platform.open_obligations("d1")) == 3

    add_request("r4", amount=5)
    agent, maker, _ = make_agent([reply(transfer(5)), reply(finish("declined", "Send a receipt first."))])
    record = agent.handle_request("r4")
    assert "proof_outstanding" in " ".join(record.guardrail_events) and len(platform.sent) == 3
    assert "3 of at most 3" in all_text(maker.calls[0]["messages"])

    # Settling one makes room again.
    _upload(platform, "r1", "p1")
    agent, *_ = make_agent([reply(tool_use("report_receipt", proof_findings=findings(25))),
                            reply(finish("sent_full", "Thanks!"))])
    assert agent.handle_request("r1").outcome is Outcome.RECEIPT_ACCEPTED
    agent, *_ = make_agent([reply(transfer(5)), reply(finish("sent_full", "Sent!"))])
    assert agent.handle_request("r4").outcome is Outcome.SENT_FULL


def test_the_engine_enforces_the_limit_for_a_compromised_maker(platform, make_agent, add_request):
    platform.dependents["d1"].spent_this_week = 0
    for rid in ("r1", "r2", "r3"):
        _pay_first(platform, make_agent, add_request, rid=rid, amount=25)
    add_request("r4", amount=5)
    from agent_layer.contracts import TransferInstruction

    result = platform.engine_transfer(TransferInstruction(
        dependent_id="d1", request_id="r4", trigger="request", amount=T(5), purpose="x", category="education",
        category_confidence=1.0, reasoning="trust me", idempotency_key="evil-r4", policy_version=3,
    ))
    assert (result.status, result.reason) == ("blocked", "proof outstanding")
