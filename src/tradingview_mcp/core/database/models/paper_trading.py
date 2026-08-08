"""Paper trading tables (schema only in Block 1; populated starting Block 2).

All monetary columns use ``Numeric`` — never floats — per the plan's
absolute rule for prices/qty/fees/PnL.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tradingview_mcp.core.database.base import Base

PRICE = Numeric(20, 8)
QTY = Numeric(24, 10)
MONEY = Numeric(20, 8)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (Index("ix_paper_orders_signal_id", "signal_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_signals.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)  # market/limit/stop/tp
    price: Mapped[str] = mapped_column(PRICE, nullable=True)
    quantity: Mapped[str] = mapped_column(QTY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class PaperFill(Base):
    __tablename__ = "paper_fills"
    __table_args__ = (Index("ix_paper_fills_order_id", "order_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_orders.id"), nullable=False
    )
    price: Mapped[str] = mapped_column(PRICE, nullable=False)
    quantity: Mapped[str] = mapped_column(QTY, nullable=False)
    fee: Mapped[str] = mapped_column(MONEY, nullable=False, default="0")
    slippage: Mapped[str] = mapped_column(PRICE, nullable=False, default="0")
    filled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (Index("ix_paper_positions_symbol_status", "symbol", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_signals.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[str] = mapped_column(PRICE, nullable=True)
    quantity: Mapped[str] = mapped_column(QTY, nullable=False, default="0")
    stop_loss: Mapped[str] = mapped_column(PRICE, nullable=True)
    take_profit_1: Mapped[str] = mapped_column(PRICE, nullable=True)
    take_profit_2: Mapped[str] = mapped_column(PRICE, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    realized_pnl: Mapped[str] = mapped_column(MONEY, nullable=False, default="0")
    fees_paid: Mapped[str] = mapped_column(MONEY, nullable=False, default="0")
    funding_paid: Mapped[str] = mapped_column(MONEY, nullable=False, default="0")
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    closed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperEquitySnapshot(Base):
    __tablename__ = "paper_equity_snapshots"
    __table_args__ = (Index("ix_paper_equity_snapshots_ts", "as_of"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    balance: Mapped[str] = mapped_column(MONEY, nullable=False)
    equity: Mapped[str] = mapped_column(MONEY, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_pnl: Mapped[str] = mapped_column(MONEY, nullable=False, default="0")


class DailyRiskState(Base):
    """One row per UTC calendar day; enforces daily-loss/trade-count limits."""

    __tablename__ = "daily_risk_state"
    __table_args__ = (Index("ix_daily_risk_state_date", "trading_date", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trading_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    starting_equity: Mapped[str] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[str] = mapped_column(MONEY, nullable=False, default="0")
    trades_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lock_reason: Mapped[str] = mapped_column(String(128), nullable=True)


class StrategyMetrics(Base):
    """Rolling per-strategy performance stats (populated by Block 2/3)."""

    __tablename__ = "strategy_metrics"
    __table_args__ = (Index("ix_strategy_metrics_setup_regime", "setup_name", "market_regime"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_name: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market_regime: Mapped[str] = mapped_column(String(32), nullable=True)
    as_of: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[str] = mapped_column(Numeric(6, 4), nullable=True)
    avg_r: Mapped[str] = mapped_column(Numeric(8, 4), nullable=True)
    expectancy: Mapped[str] = mapped_column(Numeric(10, 4), nullable=True)
    profit_factor: Mapped[str] = mapped_column(Numeric(10, 4), nullable=True)
    max_drawdown: Mapped[str] = mapped_column(Numeric(10, 4), nullable=True)
    extra_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
