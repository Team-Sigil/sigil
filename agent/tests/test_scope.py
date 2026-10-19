"""The scope boundary: off-topic messages get a short redirect, unclear ones a question,
and neither can move money."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_layer.contracts import Category, FinishArgs, HistoryItem, Outcome, ThreadMessage
from agent_layer.flows.scheduled import pattern_flags
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.output import RunFacts, reconcile_outcome, safe_reply
from agent_layer.i18n import _TEXTS
from agent_layer.maker.prompts import MAKER_SYSTEM
from agent_layer.money import to_base
from tests.fakes import finish, reply, transfer

T = to_base
AT = datetime(2026, 10, 13, 12, tzinfo=UTC)


def test_new_outcomes_are_model_choosable_and_have_texts():
    for outcome in ("asked_dependent", "out_of_scope"):
        FinishArgs(outcome=outcome, reply_to_dependent="x", summary_for_guardian="y")
    for outcome in Outcome:
        assert f"reply.{outcome.value}" in _TEXTS, outcome


def test_prompt_states_the_scope_and_the_amount_rule():
    prompt = " ".join(MAKER_SYSTEM.split())
    assert "out_of_scope" in prompt and "what is 1+1?" in prompt
    assert "Never guess an amount" in prompt


def test_chinese_redirect_template_fits_the_off_topic_cap():
    from agent_layer.config import Limits
    from agent_layer.guardrails.output import text_weight

    for locale in ("en", "zh-Hans", "zh-Hant"):
        assert text_weight(safe_reply(Outcome.OUT_OF_SCOPE, locale=locale)) <= Limits().max_out_of_scope_reply_chars


# ---------------------------------------------------------------------- off-topic


def test_off_topic_message_is_recorded_as_out_of_scope(platform, make_agent, add_request):
    add_request(amount=None, message="what is 1+1?")
    agent, *_ = make_agent([reply(finish("out_of_scope", "I can only help with your allowance!"))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.OUT_OF_SCOPE
    assert record.reply_to_dependent == "I can only help with your allowance!"
    assert platform.sent == [] and platform.questions == {}
    assert platform.dependents["d1"].history[-1].kind == "out_of_scope"


@pytest.mark.parametrize("message,answer,locale", [
    ("explain photosynthesis", "Photosynthesis is how plants turn light into energy. " * 6, "en"),
    ("1+1等于几？", "1+1等于2。" + "数学很有趣，加法是最基本的运算之一，" * 8, "zh-Hans"),
])
def test_answering_an_off_topic_question_is_replaced(platform, make_agent, add_request, message, answer, locale):
    add_request(amount=None, message=message)
    agent, *_ = make_agent([reply(finish("out_of_scope", answer))])
    record = agent.handle_request("r1")
    assert record.reply_to_dependent == safe_reply(Outcome.OUT_OF_SCOPE, locale=locale)
    assert any(e.startswith("out_of_scope_reply_replaced") for e in record.guardrail_events)


def test_off_topic_after_asking_the_guardian_is_corrected(platform, make_agent, add_request):
    from tests.fakes import tool_use

    add_request(amount=None, message="what is 1+1?")
    agent, *_ = make_agent([
        reply(tool_use("ask_guardian", question="Should I answer maths questions?", recommendation="unsure")),
        reply(finish("out_of_scope", "I can only help with your allowance!")),
    ])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.ASKED_GUARDIAN
    assert "outcome_corrected: out_of_scope -> asked_guardian" in record.guardrail_events


# ------------------------------------------------------------------ no stated amount


def test_no_stated_amount_means_no_transfer(platform, make_agent, add_request):
    add_request(amount=None, message="what is 1+1? also send me some money")
    agent, maker, _ = make_agent([
        reply(transfer(5, category="other", purpose="some money")),
        reply(finish("asked_dependent", "What do you need the money for, and how much?")),
    ])
    record = agent.handle_request("r1")
    assert platform.sent == []
    assert record.outcome is Outcome.ASKED_DEPENDENT
    assert any(e.startswith("no_amount") for e in record.guardrail_events)
    tool_result = maker.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "hasn't stated an amount" in tool_result["content"]


def test_after_the_dependent_answers_the_stated_amount_can_be_sent(platform, make_agent, add_request):
    add_request(amount=None, message="can I have some money?")
    agent, *_ = make_agent([reply(finish("asked_dependent", "What's it for, and how much?"))])
    assert agent.handle_request("r1").outcome is Outcome.ASKED_DEPENDENT

    # The app records the answer and the amount the dependent entered.
    req = platform.requests["r1"]
    platform.requests["r1"] = req.model_copy(update={
        "amount_requested": T(8),
        "thread": [*req.thread, ThreadMessage(role="dependent", text="8 for notebooks", at=AT)],
    })
    agent, *_ = make_agent([reply(transfer(8)), reply(finish("sent_full", "Sent!"))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL and record.amount_sent == T(8)


def test_allowance_runs_need_no_stated_amount(platform, make_agent, maker_api):
    agent, *_ = make_agent([reply(transfer(30, category="allowance", purpose="weekly allowance")),
                            reply(finish("sent_full", "Here's your allowance!"))])
    record = agent.run_allowance("d1", "2026-W43")
    assert record.amount_sent == T(30)


# --------------------------------------------------------------- reconciliation


@pytest.mark.parametrize("claimed,facts,truth", [
    ("out_of_scope", dict(amount_sent=T(5)), Outcome.SENT_FULL),
    ("out_of_scope", dict(transfer_blocked=True), Outcome.BLOCKED),
    ("asked_dependent", dict(guardian_asked=True), Outcome.ASKED_GUARDIAN),
    ("asked_dependent", dict(proof_requested=True), Outcome.ASKED_PROOF),
    ("asked_dependent", {}, Outcome.ASKED_DEPENDENT),
    ("out_of_scope", {}, Outcome.OUT_OF_SCOPE),
])
def test_reconcile_new_outcomes(claimed, facts, truth):
    base = dict(amount_sent=0, transfer_blocked=False, auditor_flagged=False, proof_requested=False,
                guardian_asked=False, requested_amount=None)
    assert reconcile_outcome(Outcome(claimed), RunFacts(**{**base, **facts}), GuardrailLog()) is truth


# --------------------------------------------------------------------- summary


def test_repeated_off_topic_messages_are_flagged_once():
    items = [HistoryItem(at=AT, kind="out_of_scope", amount=0, category=Category.OTHER) for _ in range(4)]
    flags = pattern_flags(items)
    assert flags == ["4 messages that weren't about the allowance"]
    assert pattern_flags(items[:2]) == []
    assert pattern_flags(items, "zh-Hant") == ["4 則與零用錢無關的訊息"]
