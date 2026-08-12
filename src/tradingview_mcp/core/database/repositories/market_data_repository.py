"""Idempotent write helpers for market-data tables.

Every "upsert" here follows the plan's rule: never overwrite newer data with
older data. Candles and derivatives snapshots therefore compare
``source_timestamp`` (not wall-clock receive time) before overwriting an
existing row.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.database.models.market_data import (
    Candle,
    DerivativesSnapshot,
    LiquidationAggregate,
    OrderbookFeatureSnapshot,
    TradeAggregate,
)


def _d(v: Any) -> str:
    """Normalise a Decimal/str/float/int into the str form we persist.

    Numeric columns accept Decimal directly; we route everything through
    Decimal first so a stray float never silently loses precision.
    """
    return str(v if isinstance(v, Decimal) else Decimal(str(v)))


async def upsert_candle(session: AsyncSession, candle: dict) -> bool:
    """Idempotent candle upsert, keyed by (symbol, interval, open_time).

    Returns True if the row was inserted/updated, False if a newer row
    already existed and this write was skipped (stale data guard).
    """
    symbol = candle["symbol"]
    interval = str(candle["interval"])
    open_time = candle["open_time"]
    source_timestamp = candle["source_timestamp"]

    existing = (
        await session.execute(
            select(Candle).where(
                Candle.symbol == symbol,
                Candle.interval == interval,
                Candle.open_time == open_time,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.source_timestamp >= source_timestamp:
        # Never overwrite newer data with older data.
        return False

    values = dict(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        open=_d(candle["open"]),
        high=_d(candle["high"]),
        low=_d(candle["low"]),
        close=_d(candle["close"]),
        volume=_d(candle["volume"]),
        turnover=_d(candle.get("turnover", 0)),
        is_closed=bool(candle.get("is_closed", True)),
        source=candle.get("source", "rest_backfill"),
        source_timestamp=source_timestamp,
        fetched_at=dt.datetime.now(dt.timezone.utc),
    )

    stmt = pg_insert(Candle).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Candle.symbol, Candle.interval, Candle.open_time],
        set_={k: v for k, v in values.items() if k not in ("symbol", "interval", "open_time")},
    )
    await session.execute(stmt)
    return True


async def insert_trade_aggregate(session: AsyncSession, agg: dict) -> None:
    stmt = pg_insert(TradeAggregate).values(
        symbol=agg["symbol"],
        bucket_seconds=agg["bucket_seconds"],
        bucket_start=agg["bucket_start"],
        buy_taker_volume=_d(agg.get("buy_taker_volume", 0)),
        sell_taker_volume=_d(agg.get("sell_taker_volume", 0)),
        delta=_d(agg.get("delta", 0)),
        trade_count=int(agg.get("trade_count", 0)),
        avg_trade_size=_d(agg.get("avg_trade_size", 0)),
        largest_trade_size=_d(agg.get("largest_trade_size", 0)),
        large_trade_count=int(agg.get("large_trade_count", 0)),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            TradeAggregate.symbol,
            TradeAggregate.bucket_seconds,
            TradeAggregate.bucket_start,
        ],
        set_=dict(
            buy_taker_volume=stmt.excluded.buy_taker_volume,
            sell_taker_volume=stmt.excluded.sell_taker_volume,
            delta=stmt.excluded.delta,
            trade_count=stmt.excluded.trade_count,
            avg_trade_size=stmt.excluded.avg_trade_size,
            largest_trade_size=stmt.excluded.largest_trade_size,
            large_trade_count=stmt.excluded.large_trade_count,
        ),
    )
    await session.execute(stmt)


async def insert_liquidation_aggregate(session: AsyncSession, agg: dict) -> None:
    stmt = pg_insert(LiquidationAggregate).values(
        symbol=agg["symbol"],
        bucket_seconds=agg["bucket_seconds"],
        bucket_start=agg["bucket_start"],
        long_liq_value=_d(agg.get("long_liq_value", 0)),
        short_liq_value=_d(agg.get("short_liq_value", 0)),
        long_liq_count=int(agg.get("long_liq_count", 0)),
        short_liq_count=int(agg.get("short_liq_count", 0)),
        largest_liq_value=_d(agg.get("largest_liq_value", 0)),
        imbalance=_d(agg["imbalance"]) if agg.get("imbalance") is not None else None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            LiquidationAggregate.symbol,
            LiquidationAggregate.bucket_seconds,
            LiquidationAggregate.bucket_start,
        ],
        set_=dict(
            long_liq_value=stmt.excluded.long_liq_value,
            short_liq_value=stmt.excluded.short_liq_value,
            long_liq_count=stmt.excluded.long_liq_count,
            short_liq_count=stmt.excluded.short_liq_count,
            largest_liq_value=stmt.excluded.largest_liq_value,
            imbalance=stmt.excluded.imbalance,
        ),
    )
    await session.execute(stmt)


async def insert_orderbook_snapshot(session: AsyncSession, snap: dict) -> None:
    """Insert one periodic order-book feature row (NOT every delta)."""
    session.add(
        OrderbookFeatureSnapshot(
            symbol=snap["symbol"],
            best_bid=_d(snap["best_bid"]) if snap.get("best_bid") is not None else None,
            best_ask=_d(snap["best_ask"]) if snap.get("best_ask") is not None else None,
            spread=_d(snap["spread"]) if snap.get("spread") is not None else None,
            spread_bps=_d(snap["spread_bps"]) if snap.get("spread_bps") is not None else None,
            mid_price=_d(snap["mid_price"]) if snap.get("mid_price") is not None else None,
            microprice=_d(snap["microprice"]) if snap.get("microprice") is not None else None,
            bid_depth=_d(snap["bid_depth"]) if snap.get("bid_depth") is not None else None,
            ask_depth=_d(snap["ask_depth"]) if snap.get("ask_depth") is not None else None,
            imbalance=_d(snap["imbalance"]) if snap.get("imbalance") is not None else None,
            depth_bands_json=snap.get("depth_bands", {}),
            is_consistent=bool(snap.get("is_consistent", True)),
            source_timestamp=snap["source_timestamp"],
            received_at=snap.get("received_at", dt.datetime.now(dt.timezone.utc)),
        )
    )


async def upsert_derivatives_snapshot(session: AsyncSession, snap: dict) -> bool:
    """Idempotent derivatives snapshot write; skips if a newer row exists
    for the same symbol within the same polling cadence bucket.

    Unlike candles, derivatives snapshots are not naturally unique-keyed
    (they're polled on an interval, not aligned to a fixed grid), so this
    simply inserts a new row and lets ``source_timestamp`` order queries.
    Idempotency here means: never insert a duplicate for the identical
    ``source_timestamp``.
    """
    existing = (
        await session.execute(
            select(DerivativesSnapshot).where(
                DerivativesSnapshot.symbol == snap["symbol"],
                DerivativesSnapshot.source_timestamp == snap["source_timestamp"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False

    session.add(
        DerivativesSnapshot(
            symbol=snap["symbol"],
            open_interest=_d(snap["open_interest"]) if snap.get("open_interest") is not None else None,
            open_interest_value=(
                _d(snap["open_interest_value"]) if snap.get("open_interest_value") is not None else None
            ),
            funding_rate=_d(snap["funding_rate"]) if snap.get("funding_rate") is not None else None,
            next_funding_time=snap.get("next_funding_time"),
            mark_price=_d(snap["mark_price"]) if snap.get("mark_price") is not None else None,
            index_price=_d(snap["index_price"]) if snap.get("index_price") is not None else None,
            basis=_d(snap["basis"]) if snap.get("basis") is not None else None,
            basis_pct=_d(snap["basis_pct"]) if snap.get("basis_pct") is not None else None,
            long_short_ratio=(
                _d(snap["long_short_ratio"]) if snap.get("long_short_ratio") is not None else None
            ),
            source_timestamp=snap["source_timestamp"],
            fetched_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    return True


async def get_latest_candle_open_time(
    session: AsyncSession, symbol: str, interval: str
) -> Optional[dt.datetime]:
    """Used by the REST poller to resume backfill idempotently after restart."""
    row = (
        await session.execute(
            select(Candle.open_time)
            .where(Candle.symbol == symbol, Candle.interval == str(interval))
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def count_candles(session: AsyncSession, symbol: str, interval: str) -> int:
    from sqlalchemy import func

    return (
        await session.execute(
            select(func.count()).select_from(Candle).where(
                Candle.symbol == symbol, Candle.interval == str(interval)
            )
        )
    ).scalar_one()
