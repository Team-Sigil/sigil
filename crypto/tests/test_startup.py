"""Startup refusals (SEC-13, SEC-14) and keeping key material out of logs (SEC-13)."""

import logging

import base58
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from solders.keypair import Keypair

from signer.config import require_devnet
from signer.keys import AgentKeyError, AgentWallet, encrypt_secret_key
from signer.main import StartupRefusedError
from tests.conftest import SECRET, World, serve


@pytest.mark.parametrize(
    "url",
    [
        "https://api.mainnet-beta.solana.com",
        "https://api.testnet.solana.com",
        "http://127.0.0.1:8899",
        "https://mainnet.helius-rpc.com/?api-key=devnet",
        "https://evil.example/devnet",
    ],
)
def test_non_devnet_rpc_refuses_to_start(world: World, url: str) -> None:
    with pytest.raises(ValidationError, match="devnet"):
        world.settings(solana_rpc_url=url)


@pytest.mark.parametrize(
    "url", ["https://api.devnet.solana.com", "https://devnet.helius-rpc.com/?api-key=x"]
)
def test_devnet_rpc_is_accepted(url: str) -> None:
    require_devnet(url)


async def test_non_devnet_genesis_refuses_to_start(world: World) -> None:
    world.chain.genesis_hash = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"  # mainnet-beta
    with pytest.raises(StartupRefusedError):
        async for _ in serve(world.app(), world.clock):
            pass


def test_key_not_matching_pubkey_refuses_to_start(world: World) -> None:
    with pytest.raises(AgentKeyError, match="does not derive"):
        world.app(agent_wallet_pubkey=str(Keypair().pubkey()))


def test_wrong_master_key_refuses_to_start(world: World) -> None:
    with pytest.raises(AgentKeyError, match="can't be decrypted"):
        world.app(signer_master_key=Fernet.generate_key().decode())


def test_unencrypted_key_refuses_to_start(world: World) -> None:
    with pytest.raises(AgentKeyError):
        world.app(agent_wallet_key_enc=base58.b58encode(bytes(world.agent)).decode())


def test_wrong_length_key_refuses_to_start(world: World) -> None:
    with pytest.raises(AgentKeyError, match="64-byte"):
        world.app(agent_wallet_key_enc=encrypt_secret_key(b"short", world.master_key))


def test_wallet_never_prints_its_secret(world: World) -> None:
    wallet = AgentWallet(world.agent)
    secret_b58 = base58.b58encode(bytes(world.agent)).decode()
    for text in (str(wallet), repr(wallet), f"{wallet}", str([wallet])):
        assert secret_b58 not in text
        assert str(world.agent.pubkey()) in text


def test_settings_repr_hides_secrets(world: World) -> None:
    settings = world.settings()
    text = repr(settings) + str(settings)
    assert world.master_key not in text
    assert settings.agent_wallet_key_enc.get_secret_value() not in text
    assert SECRET not in text


async def test_logs_contain_no_key_material(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run startup and every transfer path at DEBUG; neither the raw log records nor the
    formatted output may contain the key, its encryption, the master key or the HMAC secret."""
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage() + repr(record.args) + str(record.exc_info))

    settings = world.settings()
    app = world.app()  # configures logging
    logging.getLogger().addHandler(Collect())
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        async for client in serve(app, world.clock):
            await client.transfer(world.transfer_body())
            await client.transfer(world.transfer_body(destination_token_account=world.mint))
            world.chain.lose_send_responses = True
            await client.transfer(world.transfer_body())
            world.chain.lose_send_responses = False
            world.chain.down = True
            await client.transfer(world.transfer_body())
            await client.delegation(world.reservoir)
    finally:
        logging.getLogger().setLevel(logging.INFO)

    secret_key = bytes(world.agent)
    forbidden = [
        base58.b58encode(secret_key).decode(),
        base58.b58encode(secret_key[:32]).decode(),
        secret_key.hex(),
        secret_key[:32].hex(),
        str(list(secret_key)),
        settings.agent_wallet_key_enc.get_secret_value(),
        world.master_key,
        SECRET,
    ]
    output = "\n".join(records) + capsys.readouterr().err
    assert records, "expected some log output"
    for value in forbidden:
        assert value not in output
