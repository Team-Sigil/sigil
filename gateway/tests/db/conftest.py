"""Fixtures for tests that need the docker-compose Postgres (`docker compose up -d postgres`).

Connection URLs come from the environment or the repo-root `.env`. Without them, or when the
database is unreachable, every test in this package is skipped rather than failed.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gateway.db.models import Base, Family, User
from gateway.enums import Role

REPO_DIR = Path(__file__).resolve().parents[2]
ROOT_ENV = REPO_DIR / ".env"


def _env(name: str) -> str | None:
    if name in os.environ:
        return os.environ[name]
    if ROOT_ENV.exists():
        for line in ROOT_ENV.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip()
    return None


APP_URL = _env("TEST_DATABASE_URL")
OWNER_URL = _env("TEST_DATABASE_URL_MIGRATIONS")
SIGNER_RO_PASSWORD = _env("SIGIL_SIGNER_RO_PASSWORD")


@pytest.fixture(scope="session")
def migrated() -> Iterator[None]:
    if not APP_URL or not OWNER_URL:
        pytest.skip("TEST_DATABASE_URL / TEST_DATABASE_URL_MIGRATIONS not set")
    try:
        with create_engine(OWNER_URL).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"test database unreachable: {type(exc).__name__}")

    config = Config(str(REPO_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", OWNER_URL.replace("%", "%%"))
    command.upgrade(config, "head")
    yield


def _truncate_all() -> None:
    assert OWNER_URL is not None
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with create_engine(OWNER_URL).begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))


@pytest.fixture
async def app_engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    """Engine connected as the least-privileged app role, on a clean database."""
    assert APP_URL is not None
    _truncate_all()
    engine = create_async_engine(APP_URL)
    yield engine
    await engine.dispose()


@pytest.fixture
async def owner_engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    assert OWNER_URL is not None
    engine = create_async_engine(OWNER_URL)
    yield engine
    await engine.dispose()


@pytest.fixture
async def signer_engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    if not SIGNER_RO_PASSWORD or not APP_URL:
        pytest.skip("SIGIL_SIGNER_RO_PASSWORD not set")
    url = make_url(APP_URL).set(username="sigil_signer_ro", password=SIGNER_RO_PASSWORD)
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, expire_on_commit=False)


def _wallet() -> str:
    # Unique 44-char placeholder; DB tests do not verify keys.
    return uuid.uuid4().hex + uuid.uuid4().hex[:12]


@pytest.fixture
async def family(sessionmaker: async_sessionmaker[AsyncSession]) -> Family:
    async with sessionmaker() as session:
        guardian = User(wallet_address=_wallet(), role=Role.GUARDIAN)
        dependent = User(wallet_address=_wallet(), role=Role.DEPENDENT)
        session.add_all([guardian, dependent])
        await session.flush()
        family = Family(
            guardian_user_id=guardian.id,
            dependent_user_id=dependent.id,
            dependent_token_account=_wallet(),
            guardian_token_account=_wallet(),
            registration_signature="sig",
            registration_nonce="nonce",
        )
        session.add(family)
        await session.commit()
        return family
