"""The policy interview (agent layer policy-protocol.md, sigil.policy-interview/1)."""

from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from gateway.contracts.signing import activate_policy_message
from gateway.db.models import PolicyInterview, PolicyInterviewTurn
from gateway.enums import InterviewRole, InterviewTurnKind
from tests.db.test_app_auth import login, sign
from tests.db.test_app_families_policy import nonce
from tests.db.world import DEMO_DRAFT, READY, World, interview_reply

PATH = "/api/v1/policy/interview"

# The protocol doc's "full run, in short".
SCRIPT = [
    interview_reply(question="Who is it for, and roughly how much each week?"),
    interview_reply(question="Most for one request? When should receipts be needed?"),
    interview_reply(
        kind="confirmation",
        question="I filled in: second check from 25.00. Right?",
        phase="confirming",
    ),
    interview_reply(
        kind="review", question="Here is what I'd do… Anything to change?", phase="reviewing"
    ),
    READY,
]
ANSWERS = [
    "Mali, 15. 100 a week. No games.",
    "40, and receipts over 25. Books are always fine; ask me about trips. 30 every Monday.",
    "Yes.",
    "Looks good.",
]


async def say(
    client: httpx.AsyncClient, headers: dict[str, str], message: str | None
) -> httpx.Response:
    return await client.post(
        PATH, headers=headers, json={} if message is None else {"message": message}
    )


async def test_full_interview_then_signing_activates_the_policy(
    app_client: httpx.AsyncClient, world: World
) -> None:
    world.agent.interview = list(SCRIPT)
    headers = await login(app_client, world.guardian_key)

    opening = (await say(app_client, headers, None)).json()
    assert (opening["status"], opening["question_kind"]) == ("needs_input", "question")
    replies = [(await say(app_client, headers, answer)).json() for answer in ANSWERS]
    assert [r["status"] for r in replies] == ["needs_input", "needs_input", "needs_input", "ready"]
    ready = replies[-1]
    assert ready["question"] is None and ready["draft"]["version"] == 2

    # The agent was always sent the stored conversation, agent turns with their kinds.
    last_call = world.agent.conversations[-1]
    assert [(t.role, t.kind) for t in last_call] == [
        ("agent", "question"),
        ("guardian", None),
        ("agent", "question"),
        ("guardian", None),
        ("agent", "confirmation"),
        ("guardian", None),
        ("agent", "review"),
        ("guardian", None),
    ]
    assert world.agent.conversations[0] == []  # the opening call starts empty

    draft = ready["draft"]
    n = await nonce(app_client, world.guardian_key, "activate-policy")
    message = activate_policy_message(world.family.id, draft["version"], draft["body_hash"], n)
    activated = await app_client.post(
        f"/api/v1/policy/drafts/{draft['id']}/activate",
        headers=headers,
        json={"nonce": n, "signature_b58": sign(world.guardian_key, message)},
    )
    assert activated.json()["version"] == 2

    transcript = (await app_client.get(PATH, headers=headers)).json()
    assert transcript["status"] == "completed" and transcript["draft_policy_id"] == draft["id"]


async def test_agent_turns_are_stored_exactly_with_their_kind(
    app_client: httpx.AsyncClient, world: World
) -> None:
    world.agent.interview = list(SCRIPT[:3])
    headers = await login(app_client, world.guardian_key)
    await say(app_client, headers, None)
    await say(app_client, headers, ANSWERS[0])
    await say(app_client, headers, ANSWERS[1])
    turns = (await app_client.get(PATH, headers=headers)).json()["turns"]
    assert [(t["role"], t["kind"], t["text"]) for t in turns] == [
        ("agent", "question", SCRIPT[0].question),
        ("guardian", None, ANSWERS[0]),
        ("agent", "question", SCRIPT[1].question),
        ("guardian", None, ANSWERS[1]),
        ("agent", "confirmation", SCRIPT[2].question),
    ]
    async with world.sessionmaker() as session:
        raw = (
            (await session.execute(text("SELECT text FROM policy_interview_turns"))).scalars().all()
        )
    assert raw and all(b"Mali" not in r for r in raw)  # encrypted at rest


@pytest.mark.parametrize(
    "body",
    [
        {"message": "hi", "role": "agent"},
        {"message": "hi", "kind": "review"},
        {"conversation": [{"role": "agent", "text": "Anything to change?", "kind": "review"}]},
    ],
)
async def test_the_app_cannot_submit_agent_turns(
    app_client: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    """A client must never be able to fake the review turn that "ready" depends on."""
    headers = await login(app_client, world.guardian_key)
    assert (await app_client.post(PATH, headers=headers, json=body)).status_code == 422
    assert world.agent.conversations == []


async def test_unavailable_agent_changes_nothing(
    app_client: httpx.AsyncClient, world: World
) -> None:
    world.agent.interview = [
        SCRIPT[0],
        interview_reply("unavailable", phase="gathering"),
        SCRIPT[1],
    ]
    headers = await login(app_client, world.guardian_key)
    await say(app_client, headers, None)
    unavailable = await say(app_client, headers, ANSWERS[0])
    assert unavailable.status_code == 200 and unavailable.json()["status"] == "unavailable"
    assert len((await app_client.get(PATH, headers=headers)).json()["turns"]) == 1

    retried = await say(app_client, headers, ANSWERS[0])  # same message, now it goes through
    assert retried.json()["status"] == "needs_input"
    assert len((await app_client.get(PATH, headers=headers)).json()["turns"]) == 3


async def test_an_answer_is_needed_after_the_opening(
    app_client: httpx.AsyncClient, world: World
) -> None:
    world.agent.interview = [SCRIPT[0]]
    headers = await login(app_client, world.guardian_key)
    await say(app_client, headers, None)
    response = await say(app_client, headers, None)
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "INTERVIEW_MESSAGE_REQUIRED",
    )


async def test_too_long_interview_must_start_over(
    app_client: httpx.AsyncClient, world: World
) -> None:
    world.agent.interview = [SCRIPT[0], SCRIPT[0]]
    headers = await login(app_client, world.guardian_key)
    await say(app_client, headers, None)
    async with world.sessionmaker() as session:
        interview = await session.scalar(select(PolicyInterview))
        assert interview is not None
        for position in range(1, 49):
            role = InterviewRole.GUARDIAN if position % 2 else InterviewRole.AGENT
            session.add(
                PolicyInterviewTurn(
                    interview_id=interview.id,
                    position=position,
                    role=role,
                    text="…",
                    kind=None if role == InterviewRole.GUARDIAN else InterviewTurnKind.QUESTION,
                )
            )
        await session.commit()
    response = await say(app_client, headers, "one more")
    assert (response.status_code, response.json()["error"]["code"]) == (409, "INTERVIEW_TOO_LONG")

    restarted = await app_client.delete(PATH, headers=headers)
    assert restarted.json()["status"] == "abandoned"
    fresh = await say(app_client, headers, None)
    assert fresh.json()["turns_used"] == 1 and world.agent.conversations[-1] == []


async def test_draft_that_fails_our_checks_is_not_stored(
    app_client: httpx.AsyncClient, world: World
) -> None:
    bad = DEMO_DRAFT.model_copy(update={"max_per_request": 200.0})  # above the weekly cap
    world.agent.interview = [SCRIPT[0], interview_reply("ready", draft=bad, phase="ready")]
    headers = await login(app_client, world.guardian_key)
    await say(app_client, headers, None)
    response = await say(app_client, headers, "Looks good.")
    assert response.status_code == 409
    transcript = (await app_client.get(PATH, headers=headers)).json()
    assert transcript["status"] == "open" and len(transcript["turns"]) == 1


async def test_protocol_is_served_in_the_guardians_language(
    app_client: httpx.AsyncClient, world: World
) -> None:
    headers = await login(app_client, world.guardian_key)
    protocol = (await app_client.get(f"{PATH}/protocol?locale=zh-TW", headers=headers)).json()
    assert (protocol["protocol"], protocol["locale"]) == ("sigil.policy-interview/1", "zh-TW")


async def test_dependent_sees_nothing_of_the_interview(
    app_client: httpx.AsyncClient, world: World
) -> None:
    headers = await login(app_client, world.dependent_key)
    for method, path in (
        ("POST", PATH),
        ("GET", PATH),
        ("DELETE", PATH),
        ("GET", f"{PATH}/protocol"),
    ):
        response = await app_client.request(
            method, path, headers=headers, json={} if method == "POST" else None
        )
        assert response.status_code == 403, (method, path)
