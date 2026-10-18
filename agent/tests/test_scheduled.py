"""Agent-initiated actions: allowance, pace, reservoir, weekly summary, learning, scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime

from agent_layer.contracts import Category, GuardianAnswer, HistoryItem, Outcome
from agent_layer.flows.scheduled import (
    SummaryText,
    learning_proposals,
    pace_check,
    pattern_flags,
    reservoir_check,
    week_key,
    weekly_summary,
)
from agent_layer.money import to_base
from agent_layer.scheduler import SCHEDULE, Jobs, build_scheduler
from tests.conftest import AGREE, TODAY
from tests.fakes import ScriptedLLM, finish, reply, transfer, unavailable

T = to_base
MONDAY = date(2026, 10, 19)


def _allowance(amount=30):
    return [reply(transfer(amount, "allowance", purpose="weekly allowance", reasoning="Regular weekly allowance.")),
            reply(finish("sent_full", "Your weekly allowance is here!"))]


def test_routine_allowance_skips_auditor(platform, make_agent):
    agent, _, auditor = make_agent(_allowance(30))
    record = agent.run_allowance("d1", "2026-W43")
    assert record.outcome is Outcome.SENT_FULL and record.request_id is None
    (sent,) = platform.sent
    assert sent.trigger == "allowance" and sent.idempotency_key == "allowance:d1:2026-W43"
    assert auditor.calls == []


def test_allowance_above_usual_is_reviewed(platform, make_agent):
    agent, _, auditor = make_agent(_allowance(35), AGREE)
    agent.run_allowance("d1", "2026-W43")
    assert len(auditor.calls) == 2
    assert platform.sent[0].auditor_verdict_id is not None


def test_allowance_ceiling_enforced(platform, make_agent):
    agent, *_ = make_agent([reply(transfer(60, "allowance", reasoning="Extra generous week.")),
                            reply(finish("declined", "No allowance change this week."))])
    record = agent.run_allowance("d1", "2026-W43")
    assert platform.sent == [] and "allowance_ceiling" in " ".join(record.guardrail_events)


def test_restarted_allowance_job_never_pays_twice(platform, make_agent):
    for _ in range(2):
        agent, *_ = make_agent(_allowance(30))
        agent.run_allowance("d1", "2026-W43")
    assert len(platform.sent) == 1


def test_jobs_run_allowance_only_on_the_policy_weekday(platform, make_agent, maker_api):
    agent, maker_llm, _ = make_agent(_allowance(30))
    Jobs(agent, maker_api, maker_llm, today=lambda: TODAY).allowance_day()  # Wednesday
    assert maker_llm.calls == []
    Jobs(agent, maker_api, maker_llm, today=lambda: MONDAY).allowance_day()
    assert len(platform.sent) == 1


def test_pace_warning_only_when_spending_runs_ahead(platform, maker_api):
    assert pace_check(maker_api, "d1", TODAY) is False  # 30% spent by Wednesday
    platform.dependents["d1"].spent_this_week = T(80)
    assert pace_check(maker_api, "d1", TODAY) is True
    pace_check(maker_api, "d1", TODAY)  # re-run: deduped
    (note,) = platform.notifications.values()
    assert note.audience == "dependent" and "80%" in note.message and "20.00" in note.message


def test_pace_check_silent_when_frozen(platform, maker_api):
    platform.dependents["d1"].spent_this_week = T(80)
    platform.dependents["d1"].frozen = True
    assert pace_check(maker_api, "d1", TODAY) is False


def test_reservoir_asks_for_top_up_when_low(platform, maker_api):
    assert reservoir_check(maker_api, TODAY) == []
    platform.delegated["g1"] = T(40)
    assert reservoir_check(maker_api, TODAY) == ["g1"]
    reservoir_check(maker_api, TODAY)
    (q,) = platform.questions.values()
    assert q.recommendation == "top_up" and q.recommended_amount == T(4 * 30 - 40)
    assert q.dedupe_key == f"reservoir:g1:{week_key(TODAY)}"


def _week_items(platform, *specs):
    for kind, amount, cat, purpose in specs:
        platform.dependents["d1"].history.append(
            HistoryItem(at=datetime(2026, 10, 13, 12, tzinfo=UTC), kind=kind, amount=T(amount),
                        category=cat, purpose=purpose))


def test_pattern_flags():
    at = datetime(2026, 10, 13, tzinfo=UTC)
    items = [HistoryItem(at=at, kind="transfer", amount=T(2), category=Category.EDUCATION) for _ in range(4)]
    items += [HistoryItem(at=at, kind="blocked", amount=T(50), category=Category.GAMING) for _ in range(2)]
    flags = pattern_flags(items)
    assert any("4 requests in the 'education'" in f for f in flags)
    assert any("on Tuesday" in f for f in flags)
    assert any("2 blocked or flagged" in f for f in flags)


def test_weekly_summary_written_by_model_with_code_flags(platform, maker_api):
    _week_items(platform, *[("transfer", 3, Category.EDUCATION, "pens")] * 4)
    llm = ScriptedLLM([SummaryText(summary="Mali spent 12.00 on school supplies this week.")])
    text = weekly_summary(llm, maker_api, "d1", TODAY)
    assert text.startswith("Mali spent 12.00")
    assert "4 requests in the 'education' category" in text
    note = platform.notifications[f"summary:d1:{week_key(TODAY)}"]
    assert note.audience == "guardian" and note.recipient_id == "g1"
    assert "<untrusted_purpose>" in llm.calls[0]["messages"][0]["content"]


def test_weekly_summary_falls_back_when_model_fails_or_leaks(platform, maker_api):
    _week_items(platform, ("transfer", 5, Category.FOOD, "lunch"))
    text = weekly_summary(ScriptedLLM([unavailable()]), maker_api, "d1", TODAY)
    assert text.startswith("Mali received 5.00 of 100.00")
    platform.notifications.clear()
    leak = SummaryText(summary="Send more to 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
    assert weekly_summary(ScriptedLLM([leak]), maker_api, "d1", TODAY).startswith("Mali received")


def _answers(platform, *answers):
    platform.answers["g1"] = [
        GuardianAnswer(question_id=f"q{i}", dependent_id="d1", category=Category.EDUCATION, answer=a,
                       amount=T(10 + i), at=datetime(2026, 10, 1, tzinfo=UTC))
        for i, a in enumerate(answers)
    ]


def test_learning_proposes_stop_asking_after_consistent_approvals(platform, maker_api):
    _answers(platform, *["approved"] * 5)
    (proposal,) = learning_proposals(maker_api, "g1")
    assert "Stop asking you about 'education' requests up to 14.00" in proposal.change
    assert list(platform.proposals) == [proposal.dedupe_key]


def test_learning_silent_with_any_decline_or_too_few_answers(platform, maker_api):
    _answers(platform, "approved", "approved", "declined", "approved", "approved")
    assert learning_proposals(maker_api, "g1") == []
    _answers(platform, *["approved"] * 4)
    assert learning_proposals(maker_api, "g1") == []


def test_scheduler_registers_every_job(platform, make_agent, maker_api):
    agent, llm, _ = make_agent()
    scheduler = build_scheduler(Jobs(agent, maker_api, llm))
    assert {j.id for j in scheduler.get_jobs()} == set(SCHEDULE)
    assert all(j.max_instances == 1 for j in scheduler.get_jobs())
