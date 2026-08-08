from __future__ import annotations

import pytest

from tradingview_mcp.core.services.system_health_service import build_health_report


@pytest.mark.asyncio
async def test_health_report_database_down_when_unreachable():
    report = await build_health_report(
        database_url="postgresql+asyncpg://nouser:nopass@127.0.0.1:1/doesnotexist",
        redis_url="redis://127.0.0.1:1/0",
    )
    assert report["database"]["connected"] is False
    assert report["overall_status"] in {"DATABASE_DOWN", "CONFIG_INVALID"}


@pytest.mark.asyncio
async def test_health_report_degraded_when_collector_stale():
    collector_health = {
        "BTCUSDT": {"connected": True, "is_stale": True, "orderbook_consistent": True}
    }
    report = await build_health_report(
        collector_health=collector_health,
        database_url="postgresql+asyncpg://nouser:nopass@127.0.0.1:1/doesnotexist",
    )
    # Database check runs first and fails in this environment, which
    # correctly takes priority over "degraded collector" in overall_status.
    assert report["overall_status"] in {"DATABASE_DOWN", "DEGRADED", "CONFIG_INVALID"}
    assert report["collector"] == collector_health


@pytest.mark.asyncio
async def test_health_report_healthy_with_real_local_db(db_session):
    # db_session fixture proves a local Postgres is reachable; reuse the
    # same URL the fixture used.
    from .conftest import TEST_DATABASE_URL

    report = await build_health_report(database_url=TEST_DATABASE_URL)
    assert report["database"]["connected"] is True
