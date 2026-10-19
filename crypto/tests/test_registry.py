"""PostgresRegistry against the gateway's database, as `sigil_signer_ro`. Runs only when
SIGNER_TEST_DATABASE_URL_READONLY is set (e.g. the gateway's docker-compose Postgres)."""

import os
import uuid

import psycopg
import pytest

from signer.registry import PostgresRegistry

DATABASE_URL = os.environ.get("SIGNER_TEST_DATABASE_URL_READONLY", "")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SIGNER_TEST_DATABASE_URL_READONLY unset")


async def test_unknown_family_is_none() -> None:
    registry = PostgresRegistry(DATABASE_URL)
    await registry.open()
    try:
        assert await registry.lookup(uuid.uuid4()) is None
    finally:
        await registry.aclose()


async def test_registered_family_matches_the_view() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        row = await (
            await conn.execute(
                "SELECT family_id, dependent_token_account, guardian_token_account "
                "FROM registered_destinations "
                "WHERE dependent_token_account IS NOT NULL AND guardian_token_account IS NOT NULL "
                "LIMIT 1"
            )
        ).fetchone()
    if row is None:
        pytest.skip("no registered family in this database")
    registry = PostgresRegistry(DATABASE_URL)
    await registry.open()
    try:
        accounts = await registry.lookup(row[0])
    finally:
        await registry.aclose()
    assert accounts is not None
    assert accounts.dependent_token_account == row[1]
    assert accounts.guardian_token_account == row[2]


async def test_role_cannot_read_tables() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("SELECT * FROM families LIMIT 1")
