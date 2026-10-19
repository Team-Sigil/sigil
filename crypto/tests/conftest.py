import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from solders.keypair import Keypair

from signer.auth import sign
from signer.chain import SolanaRpc
from signer.config import AUSD, Settings
from signer.keys import encrypt_secret_key
from signer.main import create_app
from signer.registry import RegisteredAccounts
from tests.fake_chain import FakeChain, FakeTokenAccount

SECRET = "test-signer-hmac-secret"
RPC_URL = "https://api.devnet.solana.com"


def pubkey() -> str:
    return str(Keypair().pubkey())


@dataclass
class FakeRegistry:
    families: dict[UUID, RegisteredAccounts] = field(default_factory=dict)
    lookups: int = 0

    async def lookup(self, family_id: UUID) -> RegisteredAccounts | None:
        self.lookups += 1
        return self.families.get(family_id)


@dataclass
class Clock:
    now: float = field(default_factory=time.time)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class World:
    """One guardian reservoir delegated to the agent wallet, and one registered family."""

    agent: Keypair
    master_key: str
    mint: str
    guardian: str
    reservoir: str
    dependent_account: str
    family_id: UUID
    chain: FakeChain
    registry: FakeRegistry
    clock: Clock
    db_path: str

    def settings(self, **overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "signer_hmac_secret": SECRET,
            "ausd_mint": self.mint,
            "agent_wallet_pubkey": str(self.agent.pubkey()),
            "agent_wallet_key_enc": encrypt_secret_key(bytes(self.agent), self.master_key),
            "signer_master_key": self.master_key,
            "solana_rpc_url": RPC_URL,
            "database_url_readonly": "postgresql://sigil_signer_ro:pw@localhost:5432/sigil",
            "signer_db_path": self.db_path,
            "confirm_timeout_seconds": 5,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    def app(self, **overrides: Any) -> FastAPI:
        return create_app(
            self.settings(**overrides),
            registry=self.registry,
            rpc=SolanaRpc(RPC_URL, transport=self.chain.transport()),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def add_family(self, guardian_account: str | None = None) -> tuple[UUID, str]:
        family_id, account = uuid.uuid4(), pubkey()
        self.chain.token_accounts[account] = FakeTokenAccount(
            owner=pubkey(), mint=self.mint, amount=0
        )
        self.registry.families[family_id] = RegisteredAccounts(
            dependent_token_account=account,
            guardian_token_account=guardian_account or self.reservoir,
        )
        return family_id, account

    def transfer_body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "transfer_id": str(uuid.uuid4()),
            "family_id": str(self.family_id),
            "amount_units": 8 * AUSD,
            "source_token_account": self.reservoir,
            "destination_token_account": self.dependent_account,
            "mint": self.mint,
            "idempotency_key": f"req:{uuid.uuid4()}:1",
        }
        body.update(overrides)
        return body


@pytest.fixture
def world(tmp_path: Path) -> World:
    agent = Keypair()
    mint, guardian, reservoir, dependent_account = pubkey(), pubkey(), pubkey(), pubkey()
    chain = FakeChain()
    chain.token_accounts[reservoir] = FakeTokenAccount(
        owner=guardian,
        mint=mint,
        amount=1_000 * AUSD,
        delegate=str(agent.pubkey()),
        delegated_amount=500 * AUSD,
    )
    chain.token_accounts[dependent_account] = FakeTokenAccount(owner=pubkey(), mint=mint, amount=0)
    family_id = uuid.uuid4()
    registry = FakeRegistry(
        {
            family_id: RegisteredAccounts(
                dependent_token_account=dependent_account, guardian_token_account=reservoir
            )
        }
    )
    return World(
        agent=agent,
        master_key=Fernet.generate_key().decode(),
        mint=mint,
        guardian=guardian,
        reservoir=reservoir,
        dependent_account=dependent_account,
        family_id=family_id,
        chain=chain,
        registry=registry,
        clock=Clock(),
        db_path=str(tmp_path / "signer.sqlite3"),
    )


class SignerClient:
    """Talks to the signer the way the gateway does: every request HMAC-signed."""

    def __init__(self, http: httpx.AsyncClient, clock: Callable[[], float]) -> None:
        self.http = http
        self._clock = clock

    def headers(
        self,
        body: bytes,
        *,
        secret: str = SECRET,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        ts = str(int(self._clock()) if timestamp is None else timestamp)
        nonce = nonce or str(uuid.uuid4())
        return {
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(secret, ts, nonce, body),
            "Content-Type": "application/json",
        }

    async def transfer(self, body: dict[str, Any]) -> httpx.Response:
        raw = json.dumps(body).encode()
        return await self.http.post("/v1/transfer", content=raw, headers=self.headers(raw))

    async def delegation(self, token_account: str) -> httpx.Response:
        return await self.http.get(f"/v1/delegation/{token_account}", headers=self.headers(b""))


async def serve(app: FastAPI, clock: Callable[[], float]) -> AsyncIterator[SignerClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://signer") as http:
            yield SignerClient(http, clock)


@pytest.fixture
async def client(world: World) -> AsyncIterator[SignerClient]:
    async for c in serve(world.app(), world.clock):
        yield c
