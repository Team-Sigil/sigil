from datetime import UTC, datetime, timedelta

import base58
import httpx
from nacl.signing import SigningKey
from sqlalchemy import update

from gateway.app_api.sessions import create_token
from gateway.core.config import Settings
from gateway.db import audit_log
from gateway.db.models import Nonce, User
from gateway.enums import Role
from tests.db.world import World, wallet


def sign(key: SigningKey, message: str) -> str:
    return base58.b58encode(key.sign(message.encode()).signature).decode()


async def sign_in_message(client: httpx.AsyncClient, key: SigningKey) -> str:
    response = await client.post(
        "/api/v1/auth/nonce", json={"wallet": wallet(key), "purpose": "login"}
    )
    message: str = response.json()["message"]
    return message


async def login(client: httpx.AsyncClient, key: SigningKey) -> dict[str, str]:
    """Signs in with the wallet's key and returns the session's Authorization header."""
    message = await sign_in_message(client, key)
    response = await client.post(
        "/api/v1/auth/verify",
        json={"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def test_new_wallet_signs_in_as_guardian_without_family(
    app_client: httpx.AsyncClient,
) -> None:
    key = SigningKey.generate()
    message = await sign_in_message(app_client, key)
    assert message.startswith("localhost:3000 wants you to sign in with your Solana account:")
    response = await app_client.post(
        "/api/v1/auth/verify",
        json={"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)},
    )
    assert response.json()["role"] == "guardian" and response.json()["family_id"] is None


async def test_family_members_get_their_roles(app_client: httpx.AsyncClient, world: World) -> None:
    for key, role in ((world.guardian_key, "guardian"), (world.dependent_key, "dependent")):
        message = await sign_in_message(app_client, key)
        body = {"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)}
        response = (await app_client.post("/api/v1/auth/verify", json=body)).json()
        assert (response["role"], response["family_id"]) == (role, str(world.family.id))
    async with world.sessionmaker() as session:
        events = await audit_log.load_chain(session, world.family.id)
        assert [e.event_type for e in events] == ["login", "login"]


async def test_replayed_sign_in_is_refused(app_client: httpx.AsyncClient) -> None:
    """SEC-09: a captured signed message works once."""
    key = SigningKey.generate()
    message = await sign_in_message(app_client, key)
    body = {"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)}
    assert (await app_client.post("/api/v1/auth/verify", json=body)).status_code == 200
    assert (await app_client.post("/api/v1/auth/verify", json=body)).status_code == 401


async def test_message_for_another_domain_is_refused(app_client: httpx.AsyncClient) -> None:
    key = SigningKey.generate()
    message = (await sign_in_message(app_client, key)).replace("localhost:3000", "evil.example")
    body = {"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)}
    assert (await app_client.post("/api/v1/auth/verify", json=body)).status_code == 401


async def test_wrong_key_burns_the_nonce(app_client: httpx.AsyncClient) -> None:
    key = SigningKey.generate()
    message = await sign_in_message(app_client, key)
    forged = {
        "wallet": wallet(key),
        "message": message,
        "signature_b58": sign(SigningKey.generate(), message),
    }
    assert (await app_client.post("/api/v1/auth/verify", json=forged)).status_code == 401
    real = forged | {"signature_b58": sign(key, message)}
    assert (await app_client.post("/api/v1/auth/verify", json=real)).status_code == 401


async def test_expired_nonce_is_refused(app_client: httpx.AsyncClient, world: World) -> None:
    key = SigningKey.generate()
    message = await sign_in_message(app_client, key)
    async with world.sessionmaker() as session:
        await session.execute(
            update(Nonce).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    body = {"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)}
    assert (await app_client.post("/api/v1/auth/verify", json=body)).status_code == 401


async def test_nonce_for_another_purpose_cannot_sign_in(app_client: httpx.AsyncClient) -> None:
    key = SigningKey.generate()
    login_message = await sign_in_message(app_client, key)
    freeze = await app_client.post(
        "/api/v1/auth/nonce", json={"wallet": wallet(key), "purpose": "freeze"}
    )
    message = login_message.replace(
        login_message.split("Nonce: ")[1].split("\n")[0], freeze.json()["nonce"]
    )
    body = {"wallet": wallet(key), "message": message, "signature_b58": sign(key, message)}
    assert (await app_client.post("/api/v1/auth/verify", json=body)).status_code == 401


class TestTokens:
    async def test_missing_or_garbage_token_is_401(self, app_client: httpx.AsyncClient) -> None:
        for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic x"}):
            response = await app_client.get("/api/v1/me", headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_expired_token_is_401(self, app_client: httpx.AsyncClient, world: World) -> None:
        async with world.sessionmaker() as session:
            user = await session.get(User, world.family.guardian_user_id)
        assert user is not None
        settings = Settings()
        old = create_token(
            settings, user, Role.GUARDIAN, None, datetime.now(UTC) - timedelta(hours=13)
        )
        response = await app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {old}"})
        assert response.status_code == 401

    async def test_me_reflects_the_current_role(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        me = (await app_client.get("/api/v1/me", headers=headers)).json()
        assert (me["role"], me["family_id"]) == ("dependent", str(world.family.id))
