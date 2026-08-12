"""strategy_signals + signal_components tables.

These are the most heavily used tables in Block 2/3 (Strategy Engine,
Scoring Engine, MCP tools like ``get_trade_setup``). The plan specifies the
exact field list and status enum for ``strategy_signals`` — implemented
verbatim below even though Block 1 only creates the schema.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tradingview_mcp.core.database.base import Base

PRICE = Numeric(20, 8)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class SignalStatus(str, enum.Enum):
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"
    TP1 = "TP1"
    TP2 = "TP2"
    STOPPED = "STOPPED"
    CLOSED = "CLOSED"


class StrategySignal(Base):
    """A candidate or rejected trade setup produced by the Strategy Engine."""

    __tablename__ = "strategy_signals"
    __table_args__ = (
        Index("ix_strategy_signals_symbol", "symbol"),
        Index("ix_strategy_signals_created_at", "created_at"),
        Index("ix_strategy_signals_setup_name", "setup_name"),
        Index("ix_strategy_signals_status", "status"),
        Index("ix_strategy_signals_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    setup_name: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG / SHORT
    market_regime: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[SignalStatus] = mapped_column(
        Enum(SignalStatus, name="signal_status"), nullable=False, default=SignalStatus.CANDIDATE
    )
    score: Mapped[int] = mapped_column(Integer, nullable=True)

    entry_min: Mapped[str] = mapped_column(PRICE, nullable=True)
    entry_max: Mapped[str] = mapped_column(PRICE, nullable=True)
    stop_loss: Mapped[str] = mapped_column(PRICE, nullable=True)
    take_profit_1: Mapped[str] = mapped_column(PRICE, nullable=True)
    take_profit_2: Mapped[str] = mapped_column(PRICE, nullable=True)
    risk_reward_1: Mapped[str] = mapped_column(Numeric(8, 4), nullable=True)
    risk_reward_2: Mapped[str] = mapped_column(Numeric(8, 4), nullable=True)

    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str] = mapped_column(String(256), nullable=True)

    feature_snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("feature_snapshots.id"), nullable=True
    )
    tradingview_context_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    bybit_context_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    risk_context_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    rejection_reasons_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    strategy_config_version: Mapped[str] = mapped_column(String(32), nullable=True)


class SignalComponent(Base):
    """A single scoring component contributing to a ``StrategySignal.score``."""

    __tablename__ = "signal_components"
    __table_args__ = (
        Index("ix_signal_components_signal_id", "signal_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_signals.id"), nullable=False
    )
    component_name: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[str] = mapped_column(Numeric(6, 2), nullable=False)
    raw_value: Mapped[str] = mapped_column(Numeric(12, 6), nullable=True)
    contribution: Mapped[str] = mapped_column(Numeric(6, 2), nullable=False, default="0")
    notes: Mapped[str] = mapped_column(String(256), nullable=True)
