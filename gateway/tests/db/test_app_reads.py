from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gateway.core.config import Settings
from gateway.engine.transfers import TransferEngine
from gateway.enums import Role
from gateway.events import EventBus, EventType
from gateway.main import create_app
from tests.db.test_agent_api import MAKER
from tests.db.test_app_auth import login
from tests.db.test_app_requests import new_request
from tests.db.world import AGENT_WALLET, AUSD, MINT, World


def app_of(client: httpx.AsyncClient) -> FastAPI:
    transport = client._transport
    assert isinstance(transport, httpx.ASGITransport)
    app = transport.app
    assert isinstance(app, FastAPI)
    return app


class TestBudgetAndHistory:
    async def test_budget_reflects_sent_money_and_delegation(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        # /budget reads the current week, so this transfer runs on the real clock.
        engine = TransferEngine(
            world.sessionmaker, world.crypto, agent_wallet=AGENT_WALLET, mint=MINT
        )
        request = await world.add_request(10 * AUSD)
        assert (await engine.submit(world.instruction(request))).status == "sent"
        budget = (
            await app_client.get(
                "/api/v1/budget", headers=await login(app_client, world.dependent_key)
            )
        ).json()
        assert (budget["spent"], budget["reserved"], budget["weekly_cap"]) == (
            8 * AUSD,
            0,
            100 * AUSD,
        )
        assert budget["remaining"] == 92 * AUSD
        assert budget["delegated_remaining"] == 500 * AUSD

    async def test_budget_without_crypto_layer_reports_unknown_delegation(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        async def unavailable(token_account: str) -> None:
            from gateway.contracts.crypto import CryptoUnavailableError

            raise CryptoUnavailableError("down")

        world.crypto.get_delegation = unavailable  # type: ignore[method-assign,assignment]
        budget = (
            await app_client.get(
                "/api/v1/budget", headers=await login(app_client, world.guardian_key)
            )
        ).json()
        assert budget["delegated_remaining"] is None

    async def test_history_lists_transfers_with_signatures(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        request = await world.add_request(10 * AUSD)
        result = await world.engine.submit(world.instruction(request))
        history = (
            await app_client.get(
                "/api/v1/history", headers=await login(app_client, world.guardian_key)
            )
        ).json()
        assert [t["tx_signature"] for t in history["transfers"]] == [result.tx_signature]
        assert [i["kind"] for i in history["items"]] == ["transfer"]


class TestNotifications:
    async def test_recipient_sees_and_marks_read_others_cannot(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        note = {
            "audience": "guardian",
            "recipient_id": str(world.family.guardian_user_id),
            "message": "Reservoir runs out in about 2 weeks.",
        }
        await app_client.post("/agent/v1/notifications", headers=MAKER, json=note)

        guardian = await login(app_client, world.guardian_key)
        listed = (await app_client.get("/api/v1/notifications", headers=guardian)).json()
        assert [n["message"] for n in listed] == [note["message"]]
        dependent = await login(app_client, world.dependent_key)
        assert (await app_client.get("/api/v1/notifications", headers=dependent)).json() == []

        path = f"/api/v1/notifications/{listed[0]['id']}/read"
        assert (await app_client.post(path, headers=dependent)).status_code == 404
        assert (await app_client.post(path, headers=guardian)).json()["read_at"] is not None


class TestAudit:
    async def test_guardian_sees_a_verified_chain(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        guardian = await login(app_client, world.guardian_key)
        await new_request(app_client, await login(app_client, world.dependent_key))
        trail = (await app_client.get("/api/v1/audit", headers=guardian)).json()
        assert trail["chain_valid"] is True
        assert [e["event_type"] for e in trail["entries"]][:2] == ["login", "login"]

    async def test_dependent_cannot_read_the_audit_log(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        dependent = await login(app_client, world.dependent_key)
        assert (await app_client.get("/api/v1/audit", headers=dependent)).status_code == 403


class TestEvents:
    async def test_agent_activity_reaches_the_family_by_role(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        bus: EventBus = app_of(app_client).state.events
        guardian_queue = bus.subscribe(world.family.id, Role.GUARDIAN)
        dependent_queue = bus.subscribe(world.family.id, Role.DEPENDENT)

        request = await world.add_request(50 * AUSD)
        question = {
            "guardian_id": str(world.family.guardian_user_id),
            "dependent_id": world.dependent_id,
            "request_id": str(request.id),
            "question": "50 for a trip?",
            "recommendation": "approve",
            "agent_view": "Fits.",
            "dedupe_key": f"ask:{request.id}",
        }
        await app_client.post("/agent/v1/guardian-questions", headers=MAKER, json=question)
        decision = {
            "dependent_id": world.dependent_id,
            "request_id": str(request.id),
            "outcome": "asked_guardian",
            "reply_to_dependent": "I've asked your guardian.",
            "summary_for_guardian": "Asked about a trip.",
            "amount_sent": 0,
            "policy_version": 1,
        }
        await app_client.post("/agent/v1/decisions", headers=MAKER, json=decision)

        guardian_events = [guardian_queue.get_nowait().type for _ in range(guardian_queue.qsize())]
        dependent_events = [
            dependent_queue.get_nowait().type for _ in range(dependent_queue.qsize())
        ]
        assert guardian_events == [EventType.QUESTION, EventType.MESSAGE]
        assert dependent_events == [EventType.MESSAGE]  # questions stay with the guardian

    async def test_events_need_a_session(self, app_client: httpx.AsyncClient) -> None:
        assert (await app_client.get("/api/v1/events")).status_code == 401


class TestProtection:
    async def test_oversized_body_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        big = {"message": "x" * 70_000}
        response = await app_client.post("/api/v1/requests", headers=headers, json=big)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    async def test_body_without_content_length_is_counted(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(100):
                yield b"x" * 1024

        response = await app_client.post(
            "/api/v1/requests",
            headers=headers | {"Content-Type": "application/json"},
            content=chunks(),
        )
        assert response.status_code == 413

    async def test_rate_limit(self, world: World) -> None:
        """SEC-11: a burst from one caller is cut off."""
        settings = Settings(agent_wallet_pubkey=AGENT_WALLET, ausd_mint=MINT, rate_limit_per_min=3)
        app = create_app(
            settings=settings,
            sessionmaker=world.sessionmaker,
            crypto=world.crypto,
            agent=world.agent,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            codes = [(await client.get("/api/v1/me")).status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]


@pytest.mark.parametrize(("type_", "data"), [(EventType.TRANSFER, {"transfer_id": "t1"})])
def test_event_encoding_is_sse(type_: EventType, data: dict[str, str]) -> None:
    from gateway.events import Event

    assert Event(type_, data, None).encode() == 'event: transfer\ndata: {"transfer_id": "t1"}\n\n'
