from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import pytest_asyncio

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "bybit"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://trading:change_me@localhost:5432/trading_test"
)


def load_fixture(name: str):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _trading_settings_point_at_local_postgres(monkeypatch):
    """Block 3's query service calls `get_trading_settings().database_url`
    in a few places that aren't routed through the `db_session` fixture's
    explicit engine (e.g. `system_health_service.build_health_report`'s
    own connectivity probe). Point the cached settings singleton at the
    same local TEST_DATABASE_URL for the duration of each test, so those
    paths don't try to resolve the Docker Compose "postgres" hostname
    (which doesn't exist in this sandbox) and fail with a DNS error
    instead of exercising the real connectivity-check code path.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    from tradingview_mcp.core.config.trading_settings import reset_trading_settings_cache

    reset_trading_settings_cache()
    yield
    reset_trading_settings_cache()


@pytest_asyncio.fixture
async def db_session():
    """A real async Postgres session against a disposable schema.

    Skips (rather than failing) when no local Postgres is reachable, so the
    default test run stays hermetic in environments without a database —
    only the environment this Block 1 work was validated in guarantees a
    local Postgres at TEST_DATABASE_URL.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tradingview_mcp.core.database.base import Base
    import tradingview_mcp.core.database.models  # noqa: F401

    try:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        pytest.skip(f"no local Postgres reachable for integration tests: {exc!r}")
        return

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
