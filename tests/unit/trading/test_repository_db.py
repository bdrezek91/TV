"""Integration tests against a real (local) Postgres.

Skips automatically (see conftest.db_session) if no Postgres is reachable
at TEST_DATABASE_URL — these are not "stress"/real-network Bybit tests, so
they run in the default suite whenever a local Postgres is available, but
never require internet access.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from tradingview_mcp.core.database.models.market_data import Candle, DerivativesSnapshot
from tradingview_mcp.core.database.repositories.market_data_repository import (
    get_latest_candle_open_time,
    insert_liquidation_aggregate,
    insert_orderbook_snapshot,
    insert_trade_aggregate,
    upsert_candle,
    upsert_derivatives_snapshot,
    count_candles,
)


def _now():
    return dt.datetime.now(dt.timezone.utc)


@pytest.mark.asyncio
async def test_upsert_candle_inserts_then_skips_older_write(db_session):
    open_time = _now().replace(microsecond=0)
    newer_ts = _now()
    older_ts = newer_ts - dt.timedelta(seconds=30)

    candle = dict(
        symbol="BTCUSDT", interval="1", open_time=open_time,
        open="1", high="2", low="0.5", close="1.5", volume="10",
        source_timestamp=newer_ts,
    )
    written = await upsert_candle(db_session, candle)
    await db_session.commit()
    assert written is True

    stale_candle = dict(candle, close="999", source_timestamp=older_ts)
    written_again = await upsert_candle(db_session, stale_candle)
    await db_session.commit()
    assert written_again is False  # never overwrite newer with older

    row = (
        await db_session.execute(
            select(Candle).where(Candle.symbol == "BTCUSDT", Candle.open_time == open_time)
        )
    ).scalar_one()
    assert Decimal(row.close) == Decimal("1.5")  # unchanged


@pytest.mark.asyncio
async def test_upsert_candle_is_idempotent_for_same_backfill(db_session):
    open_time = _now().replace(microsecond=0)
    ts = _now()
    candle = dict(
        symbol="ETHUSDT", interval="15", open_time=open_time,
        open="1", high="2", low="0.5", close="1.5", volume="10",
        source_timestamp=ts,
    )
    await upsert_candle(db_session, candle)
    await upsert_candle(db_session, candle)  # re-run of the same backfill
    await db_session.commit()
    n = await count_candles(db_session, "ETHUSDT", "15")
    assert n == 1  # no duplicate row


@pytest.mark.asyncio
async def test_get_latest_candle_open_time(db_session):
    t1 = _now().replace(microsecond=0)
    t2 = t1 + dt.timedelta(minutes=1)
    for ot in (t1, t2):
        await upsert_candle(
            db_session,
            dict(
                symbol="SOLUSDT", interval="1", open_time=ot,
                open="1", high="2", low="0.5", close="1.5", volume="10",
                source_timestamp=ot,
            ),
        )
    await db_session.commit()
    latest = await get_latest_candle_open_time(db_session, "SOLUSDT", "1")
    assert latest == t2


@pytest.mark.asyncio
async def test_insert_trade_aggregate_and_liquidation_aggregate(db_session):
    bucket = _now().replace(microsecond=0)
    await insert_trade_aggregate(
        db_session,
        dict(symbol="BTCUSDT", bucket_seconds=1, bucket_start=bucket,
             buy_taker_volume="1.5", sell_taker_volume="0.5", delta="1.0", trade_count=3),
    )
    await insert_liquidation_aggregate(
        db_session,
        dict(symbol="BTCUSDT", bucket_seconds=1, bucket_start=bucket,
             long_liq_value="1000", short_liq_value="500", long_liq_count=1, short_liq_count=1,
             largest_liq_value="1000", imbalance="0.33"),
    )
    await db_session.commit()  # no exception == success


@pytest.mark.asyncio
async def test_orderbook_snapshot_write(db_session):
    await insert_orderbook_snapshot(
        db_session,
        dict(
            symbol="BTCUSDT", best_bid="68000", best_ask="68001", spread="1",
            spread_bps="1.5", mid_price="68000.5", microprice="68000.4",
            bid_depth="10", ask_depth="8", imbalance="0.1", depth_bands={},
            is_consistent=True, source_timestamp=_now(),
        ),
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_derivatives_snapshot_idempotent_for_same_timestamp(db_session):
    ts = _now()
    snap = dict(symbol="BTCUSDT", open_interest="100", funding_rate="0.0001", source_timestamp=ts)
    written1 = await upsert_derivatives_snapshot(db_session, snap)
    await db_session.commit()
    written2 = await upsert_derivatives_snapshot(db_session, snap)
    await db_session.commit()
    assert written1 is True
    assert written2 is False
    rows = (
        await db_session.execute(select(DerivativesSnapshot).where(DerivativesSnapshot.symbol == "BTCUSDT"))
    ).scalars().all()
    assert len(rows) == 1
