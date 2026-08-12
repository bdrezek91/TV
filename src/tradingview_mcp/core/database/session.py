"""Async engine / session factory.

Deliberately lazy: the engine is created on first use (not at import time)
so importing this module never requires a reachable database — important
for unit tests that only exercise pure logic (orderbook, aggregation,
config validation) without any Postgres available.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tradingview_mcp.core.config.trading_settings import get_trading_settings

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call."""
    global _engine
    if _engine is None or database_url is not None:
        url = database_url or get_trading_settings().database_url
        engine = create_async_engine(url, pool_pre_ping=True, future=True)
        if database_url is not None:
            return engine
        _engine = engine
    return _engine


def get_sessionmaker(
    database_url: Optional[str] = None,
) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None or database_url is not None:
        maker = async_sessionmaker(get_engine(database_url), expire_on_commit=False)
        if database_url is not None:
            return maker
        _sessionmaker = maker
    return _sessionmaker


@asynccontextmanager
async def session_scope(
    database_url: Optional[str] = None,
) -> AsyncIterator[AsyncSession]:
    """Provide a transactional scope around a series of operations."""
    maker = get_sessionmaker(database_url)
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose of the process-wide engine (graceful shutdown / test teardown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def check_database_connection(database_url: Optional[str] = None) -> bool:
    """Lightweight liveness probe used by health checks."""
    from sqlalchemy import text

    try:
        engine = get_engine(database_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
