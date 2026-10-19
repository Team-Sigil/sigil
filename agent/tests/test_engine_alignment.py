"""Behaviour that lines the agent up with the gateway's engine: pending transfers, signed
guardian overrides, the allowance cap and the run deadline."""

from __future__ import annotations

import base64
import itertools
import json

import anthropic
import httpx
import httpx2
import pytest

from agent_layer.contracts import Outcome, Proof, ProofRef, ThreadMessage
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.guardrails.output import RunFacts, reconcile_outcome, safe_reply
from agent_layer.llm import AnthropicLLM
from agent_layer.money import to_base
from mock_platform.app import MAKER_TOKEN
from tests.conftest import AGREE
from tests.fakes import findings, finish, reply, transfer

T = to_base
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt" * 30


def _image(pid: str) -> Proof:
    return Proof(id=pid, media_type="image/png", size_bytes=len(PNG), data_b64=base64.b64encode(PNG).decode())


# ----------------------------------------------------------------- pending transfers


def test_a_pending_transfer_is_reported_as_on_its_way(platform, make_agent, add_request):
    platform.signer_available = False
    add_request(amount=10)
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("declined", "Sorry, I can't send it."))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.TRANSFER_PENDING
    assert record.amount_sent == 0 and platform.sent == []
    assert record.reply_to_dependent == safe_reply(Outcome.TRANSFER_PENDING, T(10))  # not "can't send"
    assert "transfer_pending: 10.00" in record.guardrail_events
    assert platform.questions == {}  # money may still go out: no "couldn't finish" question
    assert platform.requests["r1"].already_pending == T(10)
    tool_result = maker.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Don't send anything else" in tool_result


def test_pending_money_counts_toward_the_requested_amount(platform, make_agent, add_request, base_url):
    """The double-pay the gateway review found: a new run (new idempotency key, because the
    thread grew) while the first transfer is still pending."""
    platform.signer_available = False
    add_request(amount=10)
    agent, *_ = make_agent([reply(transfer(10)), reply(finish("sent_full", "On its way!"))])
    agent.handle_request("r1")

    req = platform.requests["r1"]
    platform.requests["r1"] = req.model_copy(update={"thread": [
        *req.thread, ThreadMessage(role="dependent", text="did you send it?", at=platform._now())]})
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("declined", "It's still on its way."))])
    record = agent.handle_request("r1")
    assert "over_requested" in " ".join(record.guardrail_events)
    assert "Pending (accepted, not yet confirmed, may still go out): 10.00" in str(maker.calls[0]["messages"])

    # A compromised maker going straight to the engine with a fresh key is refused too.
    body = {"dependent_id": "d1", "request_id": "r1", "trigger": "request", "amount": T(10), "purpose": "x",
            "category": "education", "category_confidence": 1.0, "reasoning": "again",
            "idempotency_key": "evil-again", "policy_version": 3}
    resp = httpx.post(f"{base_url}/agent/v1/transfers", json=body,
                      headers={"Authorization": f"Bearer {MAKER_TOKEN}", "Idempotency-Key": "evil-again"}).json()
    assert resp["reason"] == "over requested amount"

    # The engine's retry job finishes the original transfer by its own key, exactly once.
    platform.signer_available = True
    (done,) = platform.retry_pending()
    assert done.status == "sent" and len(platform.sent) == 1
    assert (platform.requests["r1"].already_sent, platform.requests["r1"].already_pending) == (T(10), 0)
    assert platform.retry_pending() == []


def test_the_agent_never_resends_a_pending_transfer_the_engine_settles_it(platform, make_agent, add_request,
                                                                          base_url):
    platform.signer_available = False
    add_request(amount=10)
    agent, *_ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))])
    agent.handle_request("r1")
    (key,) = platform.pending

    platform.signer_available = True
    agent, *_ = make_agent([reply(transfer(10)), reply(finish("declined", "Still on its way."))])
    record = agent.handle_request("r1")  # same request state: the agent sees 10.00 pending
    assert "over_requested" in " ".join(record.guardrail_events) and platform.sent == []

    # Only the engine finishes it: a replay of the same key (or its retry job).
    (t,) = [p for k, p in platform.pending.items() if k == key]
    resp = httpx.post(f"{base_url}/agent/v1/transfers", json=t.model_dump(mode="json"),
                      headers={"Authorization": f"Bearer {MAKER_TOKEN}", "Idempotency-Key": key}).json()
    assert resp["status"] == "sent" and len(platform.sent) == 1


@pytest.mark.parametrize("claimed", ["declined", "sent_full", "asked_guardian"])
def test_pending_wins_over_the_models_claim(claimed):
    facts = RunFacts(amount_sent=0, transfer_blocked=False, auditor_flagged=False, proof_requested=False,
                     guardian_asked=claimed == "asked_guardian", requested_amount=T(10), amount_pending=T(10))
    assert reconcile_outcome(Outcome(claimed), facts, GuardrailLog()) is Outcome.TRANSFER_PENDING


# ---------------------------------------------------------- signed guardian override


def _approved(platform, add_request, *, asked, approved, proof=True):
    refs = [ProofRef(id="p1", media_type="image/png")] if proof else []
    add_request(amount=asked, proofs=refs, guardian_approved_amount=T(approved))
    if proof:
        platform.add_proof(_image("p1"))


def test_an_approved_exception_goes_above_the_per_request_max_without_review(platform, make_agent, add_request):
    _approved(platform, add_request, asked=45, approved=45)  # max per request is 40
    agent, maker, auditor = make_agent([reply(transfer(45, proof=findings(45))), reply(finish("sent_full", "Sent!"))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL and platform.sent[0].amount == T(45)
    assert auditor.calls == []  # the engine skips review for a signed override too
    assert "guardian_override: 45.00" in record.guardrail_events
    assert "The guardian approved exactly 45.00" in str(maker.calls[0]["messages"])


def test_the_override_lifts_the_weekly_cap_too(platform, make_agent, add_request):
    platform.dependents["d1"].spent_this_week = T(90)  # 10 left this week
    _approved(platform, add_request, asked=20, approved=20, proof=False)
    agent, *_ = make_agent([reply(transfer(20)), reply(finish("sent_full", "Sent!"))])
    assert agent.handle_request("r1").amount_sent == T(20)


@pytest.mark.parametrize("asked,approved,sent,code", [
    (45, 45, 44, "over_max_per_request"),   # only the exact signed amount
    (30, 45, 45, "over_requested"),         # never more than was asked
])
def test_the_override_lifts_nothing_else(platform, make_agent, add_request, asked, approved, sent, code):
    _approved(platform, add_request, asked=asked, approved=approved)
    agent, *_ = make_agent([reply(transfer(sent, proof=findings(sent))), reply(finish("declined", "No."))])
    record = agent.handle_request("r1")
    assert platform.sent == [] and code in " ".join(record.guardrail_events)


def test_the_override_still_needs_proof_and_respects_the_freeze(platform, make_agent, add_request):
    _approved(platform, add_request, asked=45, approved=45, proof=False)
    agent, *_ = make_agent([reply(transfer(45, proof=findings(45))), reply(finish("declined", "No."))])
    assert "proof_required" in " ".join(agent.handle_request("r1").guardrail_events)

    platform.dependents["d1"].frozen = True
    _approved(platform, add_request, asked=45, approved=45)
    agent, *_ = make_agent([reply(transfer(45, proof=findings(45))), reply(finish("declined", "No."))])
    assert "frozen" in " ".join(agent.handle_request("r1").guardrail_events)


# --------------------------------------------------------------------- allowance


def test_engine_caps_the_allowance_at_the_signed_amount(platform, base_url):
    body = {"dependent_id": "d1", "request_id": None, "trigger": "allowance", "amount": T(35),
            "purpose": "allowance", "category": "allowance", "category_confidence": 1.0, "reasoning": "more",
            "idempotency_key": "allowance:d1:2026-W43", "policy_version": 3}
    resp = httpx.post(f"{base_url}/agent/v1/transfers", json=body,
                      headers={"Authorization": f"Bearer {MAKER_TOKEN}",
                               "Idempotency-Key": "allowance:d1:2026-W43"}).json()
    assert resp["reason"] == "over allowance"


# ---------------------------------------------------------------- run deadline


def _clock(*times: float):
    it = itertools.chain(times, itertools.repeat(times[-1]))
    return lambda: next(it)


def test_each_model_call_gets_the_time_left(platform, make_agent, add_request):
    add_request(amount=10)
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))])
    # start at 0, first call at 0 (150 s left), second call at 100 (50 s left)
    agent._clock = _clock(0, 0, 100)
    agent.handle_request("r1")
    assert [round(c["timeout"]) for c in maker.calls] == [150, 50]


def test_running_out_of_time_ends_in_a_safe_fallback(platform, make_agent, add_request):
    add_request(amount=10)
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))])
    agent._clock = _clock(0, 0, 145)  # 5 s left at the second call: too little
    record = agent.handle_request("r1")
    # The transfer in the first turn already went out, so that is what's reported.
    assert record.outcome is Outcome.SENT_FULL and "run_deadline: 150s" in record.guardrail_events
    assert len(maker.calls) == 1


def test_running_out_of_time_before_any_decision_asks_the_guardian(platform, make_agent, add_request):
    add_request(amount=10)
    agent, maker, _ = make_agent([reply(finish("declined", "No."))])
    agent._clock = _clock(0, 200)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SAFE_FALLBACK and maker.calls == []
    assert len(platform.questions) == 1


def test_running_out_of_time_in_review_fails_closed(platform, make_agent, add_request):
    add_request(amount=10, emergency_claimed=True)  # forces a review
    agent, _, auditor = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))], AGREE)
    agent._clock = _clock(0, 0, 145)  # the maker's first call fits; the Auditor's doesn't
    record = agent.handle_request("r1")
    assert platform.sent == [] and auditor.calls == []
    assert "auditor_fail_closed: RunDeadline" in record.guardrail_events


def test_run_deadline_is_read_from_the_environment(monkeypatch):
    from agent_layer.config import Settings

    for k, v in {"PLATFORM_API_URL": "http://p", "PLATFORM_MAKER_TOKEN": "m", "PLATFORM_AUDITOR_TOKEN": "a"}.items():
        monkeypatch.setenv(k, v)
    assert Settings.from_env().limits.max_run_seconds == 150
    monkeypatch.setenv("AGENT_MAX_RUN_SECONDS", "90")
    assert Settings.from_env().limits.max_run_seconds == 90


def test_the_adapter_splits_the_time_left_over_one_retry():
    seen: list[httpx2.Request] = []
    responses = iter([
        httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
        httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ])

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return next(responses)

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    resp = AnthropicLLM(client=client).create(system="S", messages=[{"role": "user", "content": "hi"}],
                                              tools=[], timeout=40)
    assert resp.stop_reason == "end_turn" and len(seen) == 2  # one retry, even though the client had none
    assert seen[0].extensions["timeout"]["read"] == 20  # half the time left per attempt
    json.loads(seen[0].content)  # a normal request body
