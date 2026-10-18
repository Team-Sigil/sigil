import json
from typing import Any

import httpx
import pytest

from gateway.agent_client import AgentUnavailableError, ChatTurn, HttpAgentClient

# The protocol doc's example reply (agent layer policy-protocol.md).
EXAMPLE: dict[str, Any] = {
    "protocol": "sigil.policy-interview/1",
    "status": "needs_input",
    "phase": "gathering",
    "question": "What's the most for one request? Many families use 40.",
    "question_kind": "question",
    "checklist": [
        {
            "name": "weekly_cap",
            "label": "weekly budget",
            "kind": "limit",
            "required": True,
            "status": "stated",
            "summary": "100 a week",
        },
    ],
    "progress": {"required_total": 8, "required_done": 2},
    "draft": None,
    "validation": None,
    "guardrail_events": [],
    "some_future_field": "ignored",
}


def client_returning(
    body: dict[str, Any], seen: list[httpx.Request] | None = None
) -> HttpAgentClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=body)

    return HttpAgentClient("http://agent", "token", transport=httpx.MockTransport(handler))


async def turn(client: HttpAgentClient) -> Any:
    conversation = [
        ChatTurn(role="agent", text="Who is it for?", kind="question"),
        ChatTurn(role="guardian", text="Mali, 15."),
    ]
    return await client.interview_turn("g1", "d1", conversation, "en")


async def test_example_reply_parses_and_the_request_carries_kinds() -> None:
    seen: list[httpx.Request] = []
    result = await turn(client_returning(EXAMPLE, seen))
    assert (result.status, result.question_kind, result.progress.required_done) == (
        "needs_input",
        "question",
        2,
    )
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/policy-interviews/turn"
    assert sent["conversation"] == [
        {"role": "agent", "text": "Who is it for?", "kind": "question"},
        {"role": "guardian", "text": "Mali, 15."},
    ]
    assert (sent["guardian_id"], sent["dependent_id"], sent["locale"]) == ("g1", "d1", "en")


async def test_unavailable_is_a_normal_answer() -> None:
    body = EXAMPLE | {"status": "unavailable", "question": None, "question_kind": None}
    assert (await turn(client_returning(body))).status == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "sigil.policy-interview/2"},
        {"question_kind": None},
        {"question_kind": "verdict"},
        {"status": "ready", "question": None, "question_kind": None, "draft": None},
        {"status": "done"},
    ],
)
async def test_replies_outside_the_protocol_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(AgentUnavailableError):
        await turn(client_returning(EXAMPLE | change))


async def test_protocol_description_is_checked() -> None:
    good = client_returning({"protocol": "sigil.policy-interview/1", "requirements": []})
    assert (await good.interview_protocol("zh-TW"))["protocol"] == "sigil.policy-interview/1"
    with pytest.raises(AgentUnavailableError):
        await client_returning({"protocol": "other"}).interview_protocol(None)
