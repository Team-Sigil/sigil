import asyncio
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gateway.db import audit_log
from gateway.db.models import (
    GENESIS_HASH,
    BudgetCounter,
    Family,
    Policy,
    Request,
    Transfer,
    UsedProofKey,
)
from gateway.enums import AuditEvent, PolicyStatus, TransferStatus, TransferTrigger


async def _append_n(
    sessionmaker: async_sessionmaker[AsyncSession], family_id: uuid.UUID | None, n: int
) -> None:
    for i in range(n):
        async with sessionmaker() as session:
            await audit_log.append(session, family_id, AuditEvent.DECISION_RECORDED, {"i": i})
            await session.commit()


def _transfer(family: Family, key: str, status: TransferStatus = TransferStatus.SENT) -> Transfer:
    return Transfer(
        family_id=family.id,
        trigger=TransferTrigger.REQUEST,
        idempotency_key=key,
        amount=8_000_000,
        purpose="books",
        category="education",
        category_confidence=0.9,
        reasoning="fits the guidance",
        policy_version=1,
        status=status,
    )


class TestAuditLog:
    async def test_chain_links_from_genesis_and_verifies(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        await _append_n(sessionmaker, family.id, 3)
        async with sessionmaker() as session:
            chain = await audit_log.load_chain(session, family.id)
            assert [e.payload["i"] for e in chain] == [0, 1, 2]
            assert chain[0].prev_hash == GENESIS_HASH
            assert chain[1].prev_hash == chain[0].hash
            assert await audit_log.verify_chain(session, family.id)

    async def test_family_and_system_chains_are_independent(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        await _append_n(sessionmaker, None, 2)
        await _append_n(sessionmaker, family.id, 1)
        async with sessionmaker() as session:
            family_chain = await audit_log.load_chain(session, family.id)
            assert family_chain[0].prev_hash == GENESIS_HASH
            assert await audit_log.verify_chain(session, None)

    async def test_trigger_blocks_even_the_owner(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        owner_engine: AsyncEngine,
        family: Family,
    ) -> None:
        await _append_n(sessionmaker, family.id, 1)
        async with owner_engine.connect() as conn:
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.execute(text("UPDATE audit_log SET payload = '{}'"))

    async def test_tampering_is_detected(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        owner_engine: AsyncEngine,
        family: Family,
    ) -> None:
        """SEC-16 tamper test: someone with full DB access rewrites one entry."""
        await _append_n(sessionmaker, family.id, 3)
        async with owner_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
            await conn.execute(text("UPDATE audit_log SET payload = '{\"i\": 99}' WHERE id = 2"))
            await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))
        async with sessionmaker() as session:
            assert not await audit_log.verify_chain(session, family.id)

    async def test_concurrent_appends_never_fork_the_chain(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        async def writer(i: int) -> None:
            async with sessionmaker() as session:
                await audit_log.append(session, family.id, AuditEvent.DECISION_RECORDED, {"w": i})
                await asyncio.sleep(0.01)  # hold the lock while others wait
                await session.commit()

        await asyncio.gather(*(writer(i) for i in range(10)))
        async with sessionmaker() as session:
            chain = await audit_log.load_chain(session, family.id)
            assert len(chain) == 10
            assert len({e.prev_hash for e in chain}) == 10
            assert await audit_log.verify_chain(session, family.id)


def test_hash_uses_iso_utc_timestamp() -> None:
    created = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
    assert audit_log.format_timestamp(created) == "2026-10-18T08:00:00.000000Z"
    first = audit_log.compute_hash(GENESIS_HASH, "login", {"a": 1}, created)
    assert first == audit_log.compute_hash(GENESIS_HASH, "login", {"a": 1}, created)
    assert first != audit_log.compute_hash(GENESIS_HASH, "login", {"a": 2}, created)


class TestAppRolePermissions:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE audit_log SET payload = '{}'",
            "DELETE FROM audit_log",
            "UPDATE decisions SET outcome = 'sent_full'",
            "DELETE FROM messages",
            "DELETE FROM transfers",
            "DELETE FROM used_proof_keys",
        ],
    )
    async def test_append_only_and_money_rows_cannot_be_rewritten(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family, statement: str
    ) -> None:
        async with sessionmaker() as session:
            with pytest.raises(DBAPIError, match="permission denied"):
                await session.execute(text(statement))


class TestEncryptionAtRest:
    async def test_request_message_is_stored_encrypted(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        owner_engine: AsyncEngine,
        family: Family,
    ) -> None:
        """SEC-15: the dependent's words never sit in the database as plain text."""
        async with sessionmaker() as session:
            request = Request(family_id=family.id, message="I need 8 for notebooks")
            session.add(request)
            await session.commit()

        async with owner_engine.connect() as conn:
            raw = await conn.scalar(text("SELECT message FROM requests"))
        assert b"notebooks" not in raw

        async with sessionmaker() as session:
            stored = await session.scalar(select(Request).where(Request.id == request.id))
            assert stored is not None and stored.message == "I need 8 for notebooks"


class TestConstraints:
    async def test_one_transfer_per_idempotency_key(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        """SEC-07 at the database level."""
        async with sessionmaker() as session:
            session.add_all([_transfer(family, "req:r1:1"), _transfer(family, "req:r1:1")])
            with pytest.raises(IntegrityError, match="uq_transfers_idempotency_key"):
                await session.commit()

    async def test_a_proof_key_backs_only_one_transfer(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        async with sessionmaker() as session:
            first, second = _transfer(family, "req:r1:1"), _transfer(family, "req:r2:1")
            session.add_all([first, second])
            await session.flush()
            session.add_all(
                [
                    UsedProofKey(
                        guardian_user_id=family.guardian_user_id,
                        key="sha256:abc",
                        family_id=family.id,
                        transfer_id=transfer.id,
                    )
                    for transfer in (first, second)
                ]
            )
            with pytest.raises(IntegrityError, match="pk_used_proof_keys"):
                await session.commit()

    async def test_only_one_active_policy_per_family(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        async with sessionmaker() as session:
            for version in (1, 2):
                session.add(
                    Policy(
                        family_id=family.id,
                        version=version,
                        body={},
                        body_hash="h" * 64,
                        status=PolicyStatus.ACTIVE,
                    )
                )
            with pytest.raises(IntegrityError, match="uq_policies_one_active"):
                await session.commit()

    async def test_budget_counters_cannot_go_negative(
        self, sessionmaker: async_sessionmaker[AsyncSession], family: Family
    ) -> None:
        async with sessionmaker() as session:
            session.add(BudgetCounter(family_id=family.id, week_start=date(2026, 10, 12), spent=-1))
            with pytest.raises(IntegrityError, match="spent_non_negative"):
                await session.commit()


class TestSignerReadOnlyRole:
    async def test_sees_only_registered_destinations(
        self, signer_engine: AsyncEngine, family: Family
    ) -> None:
        async with signer_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM registered_destinations"))).all()
            assert [tuple(r._mapping.keys()) for r in rows] == [
                ("family_id", "dependent_token_account", "guardian_token_account")
            ]
            assert rows[0].dependent_token_account == family.dependent_token_account
            assert rows[0].guardian_token_account == family.guardian_token_account

    @pytest.mark.parametrize("table", ["families", "users", "audit_log", "transfers", "requests"])
    async def test_cannot_read_tables(
        self, signer_engine: AsyncEngine, family: Family, table: str
    ) -> None:
        async with signer_engine.connect() as conn:
            with pytest.raises(DBAPIError, match="permission denied"):
                await conn.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
