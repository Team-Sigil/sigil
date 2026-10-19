"""The family's registered accounts, from the gateway's `registered_destinations` view, read
with the `sigil_signer_ro` login (SPEC §12). The signer trusts these, not the request body.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

QUERY = (
    "SELECT dependent_token_account, guardian_token_account "
    "FROM registered_destinations WHERE family_id = %s"
)


class RegistryUnavailableError(Exception):
    """The gateway database could not be read."""


@dataclass(frozen=True)
class RegisteredAccounts:
    dependent_token_account: str
    guardian_token_account: str


class Registry(Protocol):
    async def lookup(self, family_id: UUID) -> RegisteredAccounts | None: ...


class PostgresRegistry:
    def __init__(self, database_url: str) -> None:
        # The gateway's URLs use SQLAlchemy's `postgresql+psycopg://`; psycopg wants plain.
        conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"connect_timeout": 5}
        )

    async def open(self) -> None:
        await self._pool.open(wait=False)

    async def aclose(self) -> None:
        await self._pool.close()

    async def lookup(self, family_id: UUID) -> RegisteredAccounts | None:
        try:
            async with self._pool.connection(timeout=5) as conn:
                row = await (await conn.execute(QUERY, (family_id,))).fetchone()
        except (psycopg.Error, TimeoutError) as exc:
            raise RegistryUnavailableError(type(exc).__name__) from None
        if row is None or not row[0] or not row[1]:
            return None
        return RegisteredAccounts(dependent_token_account=row[0], guardian_token_account=row[1])
