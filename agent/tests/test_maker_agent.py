"""End-to-end runs of the maker agent against the mock platform over real HTTP."""

from __future__ import annotations

import pytest

from agent_layer.config import Limits
from agent_layer.contracts import Outcome, Proof, ProofRef, ThreadMessage
from agent_layer.money import to_base
from tests.conftest import AGREE, FLAG, TODAY
from tests.fakes import all_text, findings, finish, reply, text, tool_use, transfer, unavailable

T = to_base


def test_small_routine_request_is_sent_without_review(platform, make_agent, add_request):
    add_request(amount=10)
    agent, maker, auditor = make_agent([
        reply(text("Books are fine."), transfer(10)),
        reply(finish("sent_full", "Sent 10.00 for notebooks!")),
    ])
    record = agent.handle_request("r1")

    assert record.outcome is Outcome.SENT_FULL
    assert record.amount_sent == T(10)
    assert record.reply_to_dependent == "Sent 10.00 for notebooks!"
    assert [t.amount for t in platform.sent] == [T(10)]
    assert platform.sent[0].idempotency_key == "req:r1:0"
    assert platform.sent[0].auditor_verdict_id is None
    assert auditor.calls == []  # routine: no review
    assert platform.decisions == [record]


def test_counter_offer_sends_part(platform, make_agent, add_request):
    add_request(amount=20, message="Need 20 for snacks this week")
    agent, *_ = make_agent([
        reply(transfer(10, "food", purpose="snacks")),
        reply(finish("counter_offer", "Here's 10 now; ask again next week if you need more.")),
    ])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.COUNTER_OFFER
    assert record.amount_sent == T(10)


def test_decline_moves_no_money(platform, make_agent, add_request):
    add_request(amount=15, message="Can I have 15 for concert merch?")
    agent, *_ = make_agent([reply(finish("declined", "Let's wait until next week for that."))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.DECLINED and platform.sent == []


def test_ask_for_proof(platform, make_agent, add_request):
    """Proof first is still a choice, e.g. when a quote already exists."""
    add_request(amount=30, message="I need 30 for a calculator for maths class")
    agent, *_ = make_agent([
        reply(tool_use("ask_for_proof", kind="quote", reason="A quote for the calculator, please.")),
        reply(finish("asked_proof", "Could you send a photo of the price tag?")),
    ])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.ASKED_PROOF
    assert platform.sent == []
    assert platform.proof_requests[0].kind == "quote"


def test_ask_guardian(platform, make_agent, add_request):
    add_request(amount=35, message="School trip to the museum, 35")
    agent, *_ = make_agent([
        reply(tool_use("ask_guardian", question="Approve 35 for a school museum trip?",
                       recommendation="approve", recommended_amount=35, urgency="normal")),
        reply(finish("asked_guardian", "I've asked your guardian about the trip.")),
    ])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.ASKED_GUARDIAN
    (q,) = platform.questions.values()
    assert q.recommended_amount == T(35) and q.request_id == "r1"


def test_larger_transfer_goes_through_auditor_and_carries_verdict(platform, make_agent, add_request):
    add_request(amount=30, message="Calculator for maths, receipt attached",
                proofs=[ProofRef(id="p1", media_type="text/plain")])
    platform.add_proof(Proof(id="p1", media_type="text/plain", size_bytes=40, source="ocr",
                             text="Office Mart  Casio fx-991  30.00  2026-10-13"))
    agent, maker, auditor = make_agent(
        [reply(transfer(30, reasoning="Receipt matches a school calculator.", proof=findings(30))),
         reply(finish("sent_full", "Sent!"))],
        AGREE,
    )
    record = agent.handle_request("r1")

    assert record.outcome is Outcome.SENT_FULL
    (sent,) = platform.sent
    assert sent.auditor_verdict_id is not None
    verdict = platform.verdicts[sent.auditor_verdict_id].submission
    assert verdict.amount == T(30) and verdict.idempotency_key == "req:r1:0"
    assert "over_review_threshold" in verdict.review_triggers
    # The verdict was posted with the Auditor's credential, the transfer with the maker's.
    assert ("auditor", "POST", "/agent/v1/auditor/verdicts") in platform.http_log
    assert ("maker", "POST", "/agent/v1/transfers") in platform.http_log


def test_auditor_flag_holds_money_and_asks_guardian_with_both_views(platform, make_agent, add_request):
    add_request(amount=30, message="30 for study materials (an online course)",
                proofs=[ProofRef(id="p1", media_type="text/plain")])
    platform.add_proof(Proof(id="p1", media_type="text/plain", size_bytes=30, text="GameStore gems pack 30.00",
                             source="ocr"))
    agent, maker, _ = make_agent(
        [reply(transfer(30, reasoning="Study materials are fine.", proof=findings(30, "GameStore"))),
         reply(finish("sent_full", "Sent your study money!"))],
        FLAG,
    )
    record = agent.handle_request("r1")

    assert record.outcome is Outcome.AUDITOR_FLAGGED
    assert record.amount_sent == 0 and platform.sent == []
    assert "Sent" not in record.reply_to_dependent  # the model's wrong reply was replaced
    (q,) = platform.questions.values()
    assert q.agent_view == "Study materials are fine."
    assert q.auditor_view == "Looks like a relabelled game purchase."


def test_engine_block_is_reported_and_model_adapts(platform, make_agent, add_request):
    platform.dependents["d1"].policy = platform.dependents["d1"].policy.model_copy(update={"version": 4})
    add_request(amount=10)
    agent, maker, _ = make_agent([
        reply(transfer(10)),
        reply(finish("sent_full", "Sent!")),
    ])
    # The agent read policy version 4 but the engine moved on to version 5 mid-run.
    original = agent._api.get_context

    def stale_context(dep):
        ctx = original(dep)
        platform.dependents["d1"].policy = platform.dependents["d1"].policy.model_copy(update={"version": 5})
        return ctx

    agent._api.get_context = stale_context
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.BLOCKED
    assert platform.sent == []
    assert "stale policy version" in all_text(maker.calls[1]["messages"])


def test_rejected_tool_call_is_fed_back_not_executed(platform, make_agent, add_request):
    add_request(amount=10, message="Need 10 for a game pass")
    agent, maker, _ = make_agent([
        reply(transfer(10, "gaming")),
        reply(finish("declined", "Game purchases aren't something I can send money for.")),
    ])
    record = agent.handle_request("r1")
    assert platform.sent == []
    assert record.outcome is Outcome.DECLINED
    result = maker.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "blocked the gaming category" in result["content"]


def test_invalid_tool_arguments_are_returned_to_the_model(platform, make_agent, add_request):
    add_request(amount=10)
    agent, maker, _ = make_agent([
        reply(tool_use("request_transfer", amount="ten", purpose="x")),
        reply(finish("declined", "Sorry, try again.")),
    ])
    agent.handle_request("r1")
    result = maker.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "Invalid arguments" in result["content"]


def test_missing_finish_gets_one_nudge_then_safe_fallback(platform, make_agent, add_request):
    add_request(amount=10)
    agent, maker, _ = make_agent([reply(text("Thinking...")), reply(text("Still thinking..."))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SAFE_FALLBACK
    assert "finish tool" in maker.calls[1]["messages"][-1]["content"]
    assert platform.sent == []
    assert any(k.startswith("fallback:") for k in platform.questions)  # guardian asked to decide


def test_model_outage_falls_back_safely(platform, make_agent, add_request):
    add_request(amount=10)
    agent, *_ = make_agent([unavailable()])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SAFE_FALLBACK
    assert platform.sent == []
    assert "safe_fallback: LLMUnavailable" in record.guardrail_events


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_or_truncation_falls_back(platform, make_agent, add_request, stop):
    add_request(amount=10)
    agent, *_ = make_agent([reply(text(""), stop_reason=stop)])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SAFE_FALLBACK and platform.sent == []


def test_loop_limit(platform, make_agent, add_request):
    add_request(amount=10)
    steps = [reply(tool_use("notify_guardian", message=f"note {i}")) for i in range(3)]
    agent, *_ = make_agent(steps, limits=Limits(max_loop_turns=3))
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SAFE_FALLBACK
    assert any(e.startswith("loop_limit") for e in record.guardrail_events)


def test_token_budget_exhausted_stops_before_calling_the_model(platform, make_agent, add_request):
    platform.token_budget["g1"] = 0
    add_request(amount=10)
    agent, maker, _ = make_agent([])
    record = agent.handle_request("r1")
    assert maker.calls == []
    assert record.outcome is Outcome.SAFE_FALLBACK


def test_token_usage_is_reported_per_family(platform, make_agent, add_request):
    add_request(amount=10)
    agent, *_ = make_agent([reply(transfer(10), tokens=1000), reply(finish("sent_full"), tokens=1000)])
    agent.handle_request("r1")
    assert platform.usage["g1"] == 3000


def test_outcome_claim_is_corrected_to_facts(platform, make_agent, add_request):
    add_request(amount=10)
    agent, *_ = make_agent([reply(finish("sent_full", "Sent you 10!"))])  # never called request_transfer
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.DECLINED
    assert "Sent" not in record.reply_to_dependent
    assert "outcome_corrected: sent_full -> declined" in record.guardrail_events


def test_reply_leaking_guidance_is_replaced(platform, make_agent, add_request):
    add_request(amount=10, message="Why can't I buy gems?")
    leak = "Your guardian told me: no money for mobile game top-ups or loot boxes."
    agent, *_ = make_agent([reply(finish("declined", leak))])
    record = agent.handle_request("r1")
    assert "loot boxes" not in record.reply_to_dependent
    assert any("guidance_leak" in e for e in record.guardrail_events)


def test_follow_up_turn_uses_a_new_idempotency_key(platform, make_agent, add_request):
    add_request(amount=20, message="20 for a book and bus fare",
                thread=[ThreadMessage(role="agent", text="Here's 10 now", at=f"{TODAY}T10:00:00Z"),
                        ThreadMessage(role="dependent", text="Thanks! Can I get the rest?", at=f"{TODAY}T11:00:00Z")],
                already_sent=T(10))
    agent, *_ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Here's the rest."))])
    record = agent.handle_request("r1")
    assert platform.sent[0].idempotency_key == "req:r1:2"
    assert record.amount_sent == T(10)


def test_images_are_sent_as_image_blocks_and_oversized_proofs_skipped(platform, make_agent, add_request):
    add_request(amount=10, proofs=[ProofRef(id="img", media_type="image/png"),
                                   ProofRef(id="big", media_type="image/png")])
    platform.add_proof(Proof(id="img", media_type="image/png", size_bytes=8, data_b64="iVBORw0KGgo="))
    platform.add_proof(Proof(id="big", media_type="image/png", size_bytes=50_000_000, data_b64="iVBORw0KGgo="))
    agent, maker, _ = make_agent([reply(finish("declined", "Not this time."))])
    record = agent.handle_request("r1")
    first_turn = maker.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in first_turn] == ["image", "text"]
    assert "proof_skipped: big too large" in record.guardrail_events


def test_tool_calls_after_finish_are_ignored(platform, make_agent, add_request):
    add_request(amount=10)
    agent, *_ = make_agent([reply(finish("declined", "No."), transfer(10))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.DECLINED and platform.sent == []


def test_notify_guardian_names_the_dependent(platform, make_agent, add_request):
    add_request(amount=10)
    agent, *_ = make_agent([
        reply(tool_use("notify_guardian", message="Mali has asked for school money three times today.")),
        reply(finish("declined", "Not this time.")),
    ])
    agent.handle_request("r1")
    (note,) = platform.undeduped_notifications
    assert (note.audience, note.recipient_id, note.dependent_id) == ("guardian", "g1", "d1")
