"""Market-data tables: instruments, candles, trade/liquidation aggregates,
order book feature snapshots, derivatives (OI/funding/basis) snapshots.

All monetary/quantity columns use ``Numeric`` (mapped to Python ``Decimal``)
per the plan's absolute rule of never using floats for price/qty/fee/PnL.
All timestamp columns are timezone-aware and store UTC.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tradingview_mcp.core.database.base import Base

PRICE = Numeric(20, 8)
QTY = Numeric(24, 10)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Instrument(Base):
    """A tradable instrument on Bybit (currently linear perpetuals)."""

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, default="bybit")
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="linear")
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    tick_size: Mapped[str] = mapped_column(PRICE, nullable=False, default="0.1")
    qty_step: Mapped[str] = mapped_column(QTY, nullable=False, default="0.001")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Candle(Base):
    """OHLCV candle, keyed by (symbol, interval, open_time)."""

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "open_time", name="uq_candles_symbol_interval_open_time"),
        Index("ix_candles_symbol_interval_open_time", "symbol", "interval", "open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)  # "1", "15", "60", "240"
    open_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[str] = mapped_column(PRICE, nullable=False)
    high: Mapped[str] = mapped_column(PRICE, nullable=False)
    low: Mapped[str] = mapped_column(PRICE, nullable=False)
    close: Mapped[str] = mapped_column(PRICE, nullable=False)
    volume: Mapped[str] = mapped_column(QTY, nullable=False)
    turnover: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="rest_backfill")
    source_timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class TradeAggregate(Base):
    """Aggregated taker trade flow over a fixed bucket (1s / 1m / 5m)."""

    __tablename__ = "trade_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "bucket_seconds", "bucket_start",
            name="uq_trade_aggregates_symbol_bucket",
        ),
        Index("ix_trade_aggregates_symbol_bucket_start", "symbol", "bucket_seconds", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_seconds: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 60, 300
    bucket_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    buy_taker_volume: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    sell_taker_volume: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    delta: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_trade_size: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    largest_trade_size: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    large_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class OrderbookFeatureSnapshot(Base):
    """Periodic snapshot of derived order-book statistics (not every delta)."""

    __tablename__ = "orderbook_feature_snapshots"
    __table_args__ = (
        Index("ix_orderbook_feature_snapshots_symbol_ts", "symbol", "source_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    best_bid: Mapped[str] = mapped_column(PRICE, nullable=True)
    best_ask: Mapped[str] = mapped_column(PRICE, nullable=True)
    spread: Mapped[str] = mapped_column(PRICE, nullable=True)
    spread_bps: Mapped[str] = mapped_column(Numeric(12, 4), nullable=True)
    mid_price: Mapped[str] = mapped_column(PRICE, nullable=True)
    microprice: Mapped[str] = mapped_column(PRICE, nullable=True)
    bid_depth: Mapped[str] = mapped_column(QTY, nullable=True)
    ask_depth: Mapped[str] = mapped_column(QTY, nullable=True)
    imbalance: Mapped[str] = mapped_column(Numeric(8, 6), nullable=True)
    depth_bands_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_consistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class LiquidationAggregate(Base):
    """Aggregated liquidation flow over a fixed bucket."""

    __tablename__ = "liquidation_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "bucket_seconds", "bucket_start",
            name="uq_liquidation_aggregates_symbol_bucket",
        ),
        Index("ix_liquidation_aggregates_symbol_bucket_start", "symbol", "bucket_seconds", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    long_liq_value: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    short_liq_value: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    long_liq_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    short_liq_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    largest_liq_value: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    imbalance: Mapped[str] = mapped_column(Numeric(8, 6), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class DerivativesSnapshot(Base):
    """Open interest / funding / mark-index basis / long-short ratio."""

    __tablename__ = "derivatives_snapshots"
    __table_args__ = (
        Index("ix_derivatives_snapshots_symbol_ts", "symbol", "source_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    open_interest: Mapped[str] = mapped_column(QTY, nullable=True)
    open_interest_value: Mapped[str] = mapped_column(QTY, nullable=True)
    funding_rate: Mapped[str] = mapped_column(Numeric(14, 10), nullable=True)
    next_funding_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    mark_price: Mapped[str] = mapped_column(PRICE, nullable=True)
    index_price: Mapped[str] = mapped_column(PRICE, nullable=True)
    basis: Mapped[str] = mapped_column(PRICE, nullable=True)
    basis_pct: Mapped[str] = mapped_column(Numeric(12, 6), nullable=True)
    long_short_ratio: Mapped[str] = mapped_column(Numeric(12, 6), nullable=True)
    source_timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
