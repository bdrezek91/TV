"""Feature Engine / Market Regime Classifier tables (schema only in Block 1;
populated starting Block 2)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tradingview_mcp.core.database.base import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class FeatureSnapshot(Base):
    """A point-in-time deterministic feature vector for a symbol (Block 2)."""

    __tablename__ = "feature_snapshots"
    __table_args__ = (
        Index("ix_feature_snapshots_symbol_ts", "symbol", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price_action_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    volume_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    orderbook_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    futures_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    liquidations_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    data_quality_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_fields_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    stale_fields_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_timestamps_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_tradeable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MarketRegime(Base):
    """Market regime classification result for a symbol (Block 2)."""

    __tablename__ = "market_regimes"
    __table_args__ = (
        Index("ix_market_regimes_symbol_ts", "symbol", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(Numeric(5, 4), nullable=False, default="0")
    reasons_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    warnings_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
