import uuid
from typing import Any

import httpx
from nacl.signing import SigningKey
from sqlalchemy import select

from gateway.agent_client import DraftResult
from gateway.contracts.agent import PolicyDraftValidation
from gateway.contracts.signing import activate_policy_message, register_dependent_message
from gateway.db.models import Policy
from gateway.enums import PolicyStatus
from tests.db.test_app_auth import login, sign
from tests.db.world import World, wallet


async def nonce(client: httpx.AsyncClient, key: SigningKey, purpose: str) -> str:
    response = await client.post(
        "/api/v1/auth/nonce", json={"wallet": wallet(key), "purpose": purpose}
    )
    value: str = response.json()["nonce"]
    return value


class TestFamilies:
    async def register(
        self,
        client: httpx.AsyncClient,
        guardian: SigningKey,
        signer: SigningKey | None = None,
        **overrides: Any,
    ) -> httpx.Response:
        headers = await login(client, guardian)
        n = await nonce(client, guardian, "register-dependent")
        body = {
            "dependent_wallet": wallet(),
            "dependent_token_account": wallet(),
            "guardian_token_account": wallet(),
            "dependent_name": "Mali",
            "nonce": n,
        } | overrides
        message = register_dependent_message(
            None, body["dependent_wallet"], body["dependent_token_account"], body["nonce"]
        )
        body["registration_signature_b58"] = sign(signer or guardian, message)
        return await client.post("/api/v1/families", headers=headers, json=body)

    async def test_guardian_registers_a_dependent(self, app_client: httpx.AsyncClient) -> None:
        guardian, dependent = SigningKey.generate(), SigningKey.generate()
        response = await self.register(app_client, guardian, dependent_wallet=wallet(dependent))
        assert response.status_code == 200, response.text
        family = response.json()["family"]
        assert (family["dependent_name"], family["frozen"]) == ("Mali", False)

        headers = {"Authorization": f"Bearer {response.json()['token']}"}
        me = (await app_client.get("/api/v1/me", headers=headers)).json()
        assert (me["role"], me["family_id"]) == ("guardian", family["id"])
        dependent_me = (
            await app_client.get("/api/v1/me", headers=await login(app_client, dependent))
        ).json()
        assert (dependent_me["role"], dependent_me["family_id"]) == ("dependent", family["id"])

    async def test_signature_by_another_key_is_refused(self, app_client: httpx.AsyncClient) -> None:
        response = await self.register(
            app_client, SigningKey.generate(), signer=SigningKey.generate()
        )
        assert response.status_code == 401

    async def test_second_family_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        assert (await self.register(app_client, world.guardian_key)).status_code == 409

    async def test_dependent_already_in_a_family_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        response = await self.register(
            app_client, SigningKey.generate(), dependent_wallet=wallet(world.dependent_key)
        )
        assert response.status_code == 409

    async def test_own_wallet_as_dependent_is_refused(self, app_client: httpx.AsyncClient) -> None:
        guardian = SigningKey.generate()
        response = await self.register(app_client, guardian, dependent_wallet=wallet(guardian))
        assert response.status_code == 409

    async def test_dependent_cannot_register(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        response = await self.register(app_client, world.dependent_key)
        assert response.status_code == 403


CONVERSATION = {"conversation": [{"role": "guardian", "text": "100 a week, no games."}]}


class TestPolicy:
    async def draft(self, client: httpx.AsyncClient, world: World) -> tuple[dict[str, str], Any]:
        headers = await login(client, world.guardian_key)
        response = await client.post("/api/v1/policy/drafts", headers=headers, json=CONVERSATION)
        return headers, response.json()

    async def activate(
        self,
        client: httpx.AsyncClient,
        world: World,
        headers: dict[str, str],
        draft: dict[str, Any],
        key: SigningKey | None = None,
    ) -> httpx.Response:
        n = await nonce(client, world.guardian_key, "activate-policy")
        message = activate_policy_message(world.family.id, draft["version"], draft["body_hash"], n)
        return await client.post(
            f"/api/v1/policy/drafts/{draft['id']}/activate",
            headers=headers,
            json={"nonce": n, "signature_b58": sign(key or world.guardian_key, message)},
        )

    async def test_agent_draft_becomes_a_numbered_draft_in_base_units(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        _, response = await self.draft(app_client, world)
        draft = response["draft"]
        assert draft["version"] == 2  # the world already has version 1 active
        assert draft["body"]["max_per_request"] == 40_000_000
        assert draft["body"]["allowance"] == {"amount": 30_000_000, "weekday": 0}
        assert world.agent.conversations[0][0].text == "100 a week, no games."

    async def test_signed_activation_replaces_the_active_policy(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers, response = await self.draft(app_client, world)
        activated = await self.activate(app_client, world, headers, response["draft"])
        assert activated.json()["version"] == 2

        async with world.sessionmaker() as session:
            statuses = {
                p.version: p.status
                for p in await session.scalars(
                    select(Policy).where(Policy.family_id == world.family.id)
                )
            }
        assert statuses == {1: PolicyStatus.SUPERSEDED, 2: PolicyStatus.ACTIVE}

        # Transfers made under the old version are now refused as stale.
        request = await world.add_request(10_000_000)
        result = await world.engine.submit(world.instruction(request, policy_version=1))
        assert result.reason == "stale policy version"

    async def test_activation_not_signed_by_the_guardian_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """SEC-04: neither the agent nor the server can activate a policy."""
        headers, response = await self.draft(app_client, world)
        forged = await self.activate(
            app_client, world, headers, response["draft"], key=SigningKey.generate()
        )
        assert forged.status_code == 401
        active = (await app_client.get("/api/v1/policy/active", headers=headers)).json()
        assert active["version"] == 1

    async def test_invalid_agent_draft_is_not_stored(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        world.agent.draft = DraftResult(
            draft=None, validation=PolicyDraftValidation(valid=False, errors=["no budget given"])
        )
        _, response = await self.draft(app_client, world)
        assert response["draft"] is None
        assert response["validation"]["errors"] == ["no budget given"]

    async def test_dependent_sees_limits_but_not_guidance(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        guardian = (
            await app_client.get(
                "/api/v1/policy/active", headers=await login(app_client, world.guardian_key)
            )
        ).json()
        dependent = (
            await app_client.get(
                "/api/v1/policy/active", headers=await login(app_client, world.dependent_key)
            )
        ).json()
        assert "guidance" in guardian["body"] and guardian["body_hash"]
        assert "guidance" not in dependent["body"] and dependent["body_hash"] is None
        assert dependent["body"]["weekly_cap"] == guardian["body"]["weekly_cap"]

    async def test_dependent_cannot_draft_or_read_drafts(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        drafted = await app_client.post("/api/v1/policy/drafts", headers=headers, json=CONVERSATION)
        assert drafted.status_code == 403
        read = await app_client.get(f"/api/v1/policy/drafts/{uuid.uuid4()}", headers=headers)
        assert read.status_code == 403
