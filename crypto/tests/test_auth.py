"""SEC-03: every request is authenticated with the shared HMAC secret, a fresh timestamp and
an unused nonce."""

import json

from tests.conftest import SignerClient, World


async def test_signed_request_is_accepted(client: SignerClient, world: World) -> None:
    response = await client.delegation(world.reservoir)
    assert response.status_code == 200


async def test_bad_signature_is_rejected(client: SignerClient, world: World) -> None:
    raw = json.dumps(world.transfer_body()).encode()
    headers = client.headers(raw, secret="not-the-shared-secret")
    response = await client.http.post("/v1/transfer", content=raw, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error_code": "SIGNER_REJECTED"}
    assert world.chain.send_calls == 0


async def test_body_must_match_signature(client: SignerClient, world: World) -> None:
    raw = json.dumps(world.transfer_body()).encode()
    headers = client.headers(raw)
    tampered = json.dumps(world.transfer_body(amount_units=199_000_000)).encode()
    response = await client.http.post("/v1/transfer", content=tampered, headers=headers)
    assert response.status_code == 401


async def test_old_timestamp_is_rejected(client: SignerClient, world: World) -> None:
    headers = client.headers(b"", timestamp=int(world.clock.now) - 31)
    response = await client.http.get(f"/v1/delegation/{world.reservoir}", headers=headers)
    assert response.status_code == 401


async def test_future_timestamp_is_rejected(client: SignerClient, world: World) -> None:
    headers = client.headers(b"", timestamp=int(world.clock.now) + 31)
    response = await client.http.get(f"/v1/delegation/{world.reservoir}", headers=headers)
    assert response.status_code == 401


async def test_reused_nonce_is_rejected(client: SignerClient, world: World) -> None:
    headers = client.headers(b"")
    first = await client.http.get(f"/v1/delegation/{world.reservoir}", headers=headers)
    replay = await client.http.get(f"/v1/delegation/{world.reservoir}", headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 401


async def test_replayed_transfer_is_rejected_even_with_fresh_body_signature(
    client: SignerClient, world: World
) -> None:
    raw = json.dumps(world.transfer_body()).encode()
    headers = client.headers(raw)
    assert (await client.http.post("/v1/transfer", content=raw, headers=headers)).status_code == 200
    replay = await client.http.post("/v1/transfer", content=raw, headers=headers)
    assert replay.status_code == 401


async def test_missing_headers_are_rejected(client: SignerClient, world: World) -> None:
    response = await client.http.get(f"/v1/delegation/{world.reservoir}")
    assert response.status_code == 401
