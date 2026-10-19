from tests.conftest import SignerClient, World, pubkey
from tests.fake_chain import FakeTokenAccount


async def test_reports_owner_delegate_and_integer_amount(
    client: SignerClient, world: World
) -> None:
    response = await client.delegation(world.reservoir)
    assert response.status_code == 200
    assert response.json() == {
        "token_account": world.reservoir,
        "owner": world.guardian,
        "delegate": str(world.agent.pubkey()),
        "delegated_amount_units": 500_000_000,
    }


async def test_read_is_fresh_after_revoke(client: SignerClient, world: World) -> None:
    assert (await client.delegation(world.reservoir)).json()["delegated_amount_units"] > 0
    account = world.chain.token_accounts[world.reservoir]
    account.delegate, account.delegated_amount = None, 0  # `spl-token revoke`
    response = await client.delegation(world.reservoir)
    assert response.status_code == 200
    assert response.json()["delegate"] is None
    assert response.json()["delegated_amount_units"] == 0


async def test_never_approved_account(client: SignerClient, world: World) -> None:
    account = pubkey()
    world.chain.token_accounts[account] = FakeTokenAccount(
        owner=pubkey(), mint=world.mint, amount=1
    )
    body = (await client.delegation(account)).json()
    assert body["delegate"] is None
    assert body["delegated_amount_units"] == 0


async def test_unknown_account_is_404(client: SignerClient) -> None:
    response = await client.delegation(pubkey())
    assert response.status_code == 404
    assert response.json() == {"error_code": "UNKNOWN_ACCOUNT"}


async def test_non_token_account_is_404(client: SignerClient, world: World) -> None:
    wallet = pubkey()
    world.chain.other_accounts.add(wallet)
    assert (await client.delegation(wallet)).status_code == 404


async def test_malformed_address_is_404(client: SignerClient) -> None:
    assert (await client.delegation("not-a-pubkey")).status_code == 404


async def test_chain_down_is_503(client: SignerClient, world: World) -> None:
    world.chain.down = True
    response = await client.delegation(world.reservoir)
    assert response.status_code == 503
    assert response.json() == {"error_code": "CHAIN_UNAVAILABLE"}
