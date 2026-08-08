"""Read-only queries backing the Block 3 MCP tools.

Every function here is a plain SELECT against tables Block 1/2 already
populate — no writes, no business logic (that stays in
`core/services/trading_query_service.py` and the `core/analysis/*`
modules). Kept separate from the write-side repositories so the read path
used by MCP tool handlers is easy to reason about and test in isolation.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.database.models.market_data import (
    Candle,
    DerivativesSnapshot,
    LiquidationAggregate,
    OrderbookFeatureSnapshot,
    TradeAggregate,
)
from tradingview_mcp.core.database.models.analysis import FeatureSnapshot, MarketRegime
from tradingview_mcp.core.database.models.paper_trading import (
    DailyRiskState,
    PaperEquitySnapshot,
    PaperPosition,
)
from tradingview_mcp.core.database.models.signals import SignalStatus, StrategySignal


async def get_latest_orderbook_snapshot(session: AsyncSession, symbol: str) -> Optional[OrderbookFeatureSnapshot]:
    return (
        await session.execute(
            select(OrderbookFeatureSnapshot)
            .where(OrderbookFeatureSnapshot.symbol == symbol)
            .order_by(OrderbookFeatureSnapshot.source_timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_latest_derivatives_snapshot(session: AsyncSession, symbol: str) -> Optional[DerivativesSnapshot]:
    return (
        await session.execute(
            select(DerivativesSnapshot)
            .where(DerivativesSnapshot.symbol == symbol)
            .order_by(DerivativesSnapshot.source_timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_recent_trade_aggregates(
    session: AsyncSession, symbol: str, bucket_seconds: int, limit: int = 30
) -> Sequence[TradeAggregate]:
    rows = (
        await session.execute(
            select(TradeAggregate)
            .where(TradeAggregate.symbol == symbol, TradeAggregate.bucket_seconds == bucket_seconds)
            .order_by(TradeAggregate.bucket_start.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def get_recent_liquidation_aggregates(
    session: AsyncSession, symbol: str, bucket_seconds: int, limit: int = 30
) -> Sequence[LiquidationAggregate]:
    rows = (
        await session.execute(
            select(LiquidationAggregate)
            .where(LiquidationAggregate.symbol == symbol, LiquidationAggregate.bucket_seconds == bucket_seconds)
            .order_by(LiquidationAggregate.bucket_start.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def get_recent_candles(session: AsyncSession, symbol: str, interval: str, limit: int = 250) -> Sequence[Candle]:
    rows = (
        await session.execute(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.interval == interval)
            .order_by(Candle.open_time.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def get_latest_market_regime(session: AsyncSession, symbol: str) -> Optional[MarketRegime]:
    return (
        await session.execute(
            select(MarketRegime).where(MarketRegime.symbol == symbol).order_by(MarketRegime.as_of.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def get_latest_feature_snapshot(session: AsyncSession, symbol: str) -> Optional[FeatureSnapshot]:
    return (
        await session.execute(
            select(FeatureSnapshot)
            .where(FeatureSnapshot.symbol == symbol)
            .order_by(FeatureSnapshot.as_of.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_signal_by_id(session: AsyncSession, signal_id: UUID) -> Optional[StrategySignal]:
    return (
        await session.execute(select(StrategySignal).where(StrategySignal.id == signal_id))
    ).scalar_one_or_none()


async def list_ranked_candidate_signals(
    session: AsyncSession,
    symbols: Sequence[str],
    *,
    minimum_score: int = 0,
    maximum_results: int = 10,
    statuses: Sequence[SignalStatus] = (SignalStatus.CANDIDATE, SignalStatus.ACTIVE),
    since: Optional[dt.datetime] = None,
) -> Sequence[StrategySignal]:
    query = select(StrategySignal).where(
        StrategySignal.symbol.in_(symbols),
        StrategySignal.status.in_(statuses),
        StrategySignal.score.is_not(None),
        StrategySignal.score >= minimum_score,
    )
    if since is not None:
        query = query.where(StrategySignal.created_at >= since)
    query = query.order_by(StrategySignal.score.desc()).limit(maximum_results)
    return (await session.execute(query)).scalars().all()


async def list_rejected_signals(
    session: AsyncSession, symbols: Sequence[str], *, since: Optional[dt.datetime] = None, limit: int = 50
) -> Sequence[StrategySignal]:
    query = select(StrategySignal).where(
        StrategySignal.symbol.in_(symbols), StrategySignal.status == SignalStatus.REJECTED
    )
    if since is not None:
        query = query.where(StrategySignal.created_at >= since)
    query = query.order_by(StrategySignal.created_at.desc()).limit(limit)
    return (await session.execute(query)).scalars().all()


async def get_open_positions(session: AsyncSession) -> Sequence[PaperPosition]:
    return (
        await session.execute(select(PaperPosition).where(PaperPosition.status == "OPEN"))
    ).scalars().all()


async def get_closed_positions_since(session: AsyncSession, since: dt.datetime) -> Sequence[PaperPosition]:
    return (
        await session.execute(
            select(PaperPosition).where(PaperPosition.status == "CLOSED", PaperPosition.closed_at >= since)
        )
    ).scalars().all()


async def get_latest_equity_snapshot(session: AsyncSession) -> Optional[PaperEquitySnapshot]:
    return (
        await session.execute(
            select(PaperEquitySnapshot).order_by(PaperEquitySnapshot.as_of.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def get_daily_risk_state_for(session: AsyncSession, trading_date: dt.date) -> Optional[DailyRiskState]:
    return (
        await session.execute(
            select(DailyRiskState).where(DailyRiskState.trading_date == trading_date)
        )
    ).scalar_one_or_none()
