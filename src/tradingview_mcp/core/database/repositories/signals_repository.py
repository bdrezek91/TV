"""Persistence for `strategy_signals` / `signal_components` /
`feature_snapshots` / `market_regimes`, and the small `daily_risk_state`
queries the hard gates / risk engine need (duplicate-signal check, trade
count today, consecutive losses).

Every setup — approved or rejected — is persisted, per the plan's rule
that rejected setups are never silently dropped.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.database.models.analysis import FeatureSnapshot, MarketRegime
from tradingview_mcp.core.database.models.paper_trading import DailyRiskState
from tradingview_mcp.core.database.models.signals import SignalComponent, SignalStatus, StrategySignal


def _d(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v if isinstance(v, Decimal) else Decimal(str(v)))


async def save_feature_snapshot(session: AsyncSession, symbol: str, snapshot: dict) -> int:
    row = FeatureSnapshot(
        symbol=symbol,
        as_of=snapshot["as_of"],
        price_action_json=_json_safe(snapshot.get("price_action", {})),
        volume_json=_json_safe(snapshot.get("volume", {})),
        orderbook_json=_json_safe(snapshot.get("orderbook", {})),
        futures_json=_json_safe(snapshot.get("futures", {})),
        liquidations_json=_json_safe(snapshot.get("liquidations", {})),
        data_quality_score=snapshot.get("data_quality_score", 0),
        missing_fields_json=snapshot.get("missing_fields", []),
        stale_fields_json=snapshot.get("stale_fields", []),
        source_timestamps_json=snapshot.get("source_timestamps", {}),
        is_tradeable=bool(snapshot.get("is_tradeable", False)),
    )
    session.add(row)
    await session.flush()
    return row.id


def _json_safe(d: dict) -> dict:
    """Recursively stringify Decimal/datetime values so the dict is valid
    JSONB payload."""
    out = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, dt.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _json_safe(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [
                str(x) if isinstance(x, Decimal) else (x.isoformat() if isinstance(x, dt.datetime) else x)
                for x in v
            ]
        else:
            out[k] = v
    return out


async def save_market_regime(session: AsyncSession, symbol: str, as_of: dt.datetime, regime_result: dict) -> int:
    row = MarketRegime(
        symbol=symbol,
        as_of=as_of,
        regime=regime_result["regime"],
        confidence=str(regime_result.get("confidence", 0)),
        reasons_json=regime_result.get("reasons", []),
        warnings_json=regime_result.get("warnings", []),
    )
    session.add(row)
    await session.flush()
    return row.id


async def has_active_duplicate(session: AsyncSession, symbol: str, setup_name: str, direction: str) -> bool:
    active_statuses = (SignalStatus.CANDIDATE, SignalStatus.ACTIVE, SignalStatus.FILLED)
    row = (
        await session.execute(
            select(StrategySignal.id).where(
                StrategySignal.symbol == symbol,
                StrategySignal.setup_name == setup_name,
                StrategySignal.direction == direction,
                StrategySignal.status.in_(active_statuses),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def save_signal(
    session: AsyncSession,
    *,
    symbol: str,
    setup_name: str,
    direction: str,
    market_regime: Optional[str],
    status: SignalStatus,
    score: Optional[int],
    entry_min: Optional[Decimal],
    entry_max: Optional[Decimal],
    stop_loss: Optional[Decimal],
    take_profit_1: Optional[Decimal],
    take_profit_2: Optional[Decimal],
    risk_reward_1: Optional[Decimal],
    risk_reward_2: Optional[Decimal],
    expires_at: Optional[dt.datetime],
    invalidation_reason: Optional[str],
    feature_snapshot_id: Optional[int],
    tradingview_context: dict,
    bybit_context: dict,
    risk_context: dict,
    rejection_reasons: list,
    strategy_config_version: Optional[str],
    components: Optional[list] = None,
) -> UUID:
    row = StrategySignal(
        symbol=symbol,
        setup_name=setup_name,
        direction=direction,
        market_regime=market_regime,
        status=status,
        score=score,
        entry_min=_d(entry_min),
        entry_max=_d(entry_max),
        stop_loss=_d(stop_loss),
        take_profit_1=_d(take_profit_1),
        take_profit_2=_d(take_profit_2),
        risk_reward_1=_d(risk_reward_1),
        risk_reward_2=_d(risk_reward_2),
        expires_at=expires_at,
        invalidation_reason=invalidation_reason,
        feature_snapshot_id=feature_snapshot_id,
        tradingview_context_json=_json_safe(tradingview_context),
        bybit_context_json=_json_safe(bybit_context),
        risk_context_json=_json_safe(risk_context),
        rejection_reasons_json=rejection_reasons,
        strategy_config_version=strategy_config_version,
    )
    session.add(row)
    await session.flush()

    for comp in components or []:
        session.add(
            SignalComponent(
                signal_id=row.id,
                component_name=comp["name"],
                weight=str(comp["weight"]),
                raw_value=str(comp["fraction"]),
                contribution=str(comp["contribution"]),
            )
        )
    await session.flush()
    return row.id


async def get_or_create_daily_risk_state(
    session: AsyncSession, trading_date: dt.date, starting_equity: Decimal
) -> DailyRiskState:
    row = (
        await session.execute(
            select(DailyRiskState).where(DailyRiskState.trading_date == trading_date)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = DailyRiskState(
        trading_date=trading_date,
        starting_equity=str(starting_equity),
        realized_pnl="0",
        trades_count=0,
        consecutive_losses=0,
        is_locked=False,
    )
    session.add(row)
    await session.flush()
    return row


async def record_trade_result(
    session: AsyncSession, trading_date: dt.date, pnl: Decimal, max_daily_loss_pct: Decimal
) -> DailyRiskState:
    state = await get_or_create_daily_risk_state(session, trading_date, Decimal("0"))
    state.trades_count += 1
    state.realized_pnl = str(Decimal(state.realized_pnl) + pnl)
    if pnl < 0:
        state.consecutive_losses += 1
    else:
        state.consecutive_losses = 0

    starting_equity = Decimal(state.starting_equity) if Decimal(state.starting_equity) > 0 else Decimal("1")
    loss_pct = (-Decimal(state.realized_pnl) / starting_equity * Decimal("100")) if Decimal(state.realized_pnl) < 0 else Decimal("0")
    if loss_pct >= max_daily_loss_pct:
        state.is_locked = True
        state.lock_reason = f"daily loss {loss_pct:.4f}% reached limit {max_daily_loss_pct}%"
    await session.flush()
    return state
