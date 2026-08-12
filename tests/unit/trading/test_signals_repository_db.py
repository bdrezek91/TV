"""Integration tests against a real (local) Postgres — see
tests/unit/trading/conftest.py::db_session (auto-skips if unreachable)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradingview_mcp.core.database.models.signals import SignalStatus
from tradingview_mcp.core.database.repositories.paper_trading_repository import (
    close_position,
    create_paper_order,
    open_position,
    record_equity_snapshot,
    record_fill,
)
from tradingview_mcp.core.database.repositories.signals_repository import (
    get_or_create_daily_risk_state,
    has_active_duplicate,
    record_trade_result,
    save_feature_snapshot,
    save_market_regime,
    save_signal,
)


def _now():
    return dt.datetime.now(dt.timezone.utc)


@pytest.mark.asyncio
async def test_save_feature_snapshot_and_market_regime(db_session):
    snap = {
        "as_of": _now(), "price_action": {"last_close": Decimal("100")},
        "volume": {}, "orderbook": {}, "futures": {}, "liquidations": {},
        "data_quality_score": 90, "missing_fields": [], "stale_fields": [],
        "source_timestamps": {}, "is_tradeable": True,
    }
    fs_id = await save_feature_snapshot(db_session, "BTCUSDT", snap)
    await db_session.commit()
    assert fs_id > 0

    regime_id = await save_market_regime(
        db_session, "BTCUSDT", _now(), {"regime": "TREND_UP", "confidence": 0.8, "reasons": [], "warnings": []}
    )
    await db_session.commit()
    assert regime_id > 0


@pytest.mark.asyncio
async def test_save_signal_candidate_and_rejected(db_session):
    signal_id = await save_signal(
        db_session,
        symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG",
        market_regime="TREND_UP", status=SignalStatus.CANDIDATE, score=82,
        entry_min=Decimal("68200"), entry_max=Decimal("68400"), stop_loss=Decimal("67550"),
        take_profit_1=Decimal("69600"), take_profit_2=Decimal("70800"),
        risk_reward_1=Decimal("2.0"), risk_reward_2=Decimal("3.6"),
        expires_at=_now() + dt.timedelta(minutes=60), invalidation_reason="15m close below 67550",
        feature_snapshot_id=None, tradingview_context={"trend_4h": "BULLISH"},
        bybit_context={"cvd": "positive"}, risk_context={"risk_amount": "25.00"},
        rejection_reasons=[], strategy_config_version="v1",
        components=[{"name": "trend_alignment", "weight": Decimal("15"), "fraction": Decimal("1"), "contribution": Decimal("15")}],
    )
    await db_session.commit()
    assert signal_id is not None

    is_dup = await has_active_duplicate(db_session, "BTCUSDT", "trend_pullback", "LONG")
    assert is_dup is True  # the CANDIDATE we just saved counts as active

    rejected_id = await save_signal(
        db_session,
        symbol="BTCUSDT", setup_name="breakout_retest", direction="SHORT",
        market_regime="CHAOTIC", status=SignalStatus.REJECTED, score=None,
        entry_min=None, entry_max=None, stop_loss=None, take_profit_1=None, take_profit_2=None,
        risk_reward_1=None, risk_reward_2=None, expires_at=None, invalidation_reason=None,
        feature_snapshot_id=None, tradingview_context={}, bybit_context={}, risk_context={},
        rejection_reasons=["market regime CHAOTIC is never tradeable"], strategy_config_version="v1",
    )
    await db_session.commit()
    assert rejected_id is not None


@pytest.mark.asyncio
async def test_daily_risk_state_tracks_losses_and_locks(db_session):
    today = _now().date()
    state = await get_or_create_daily_risk_state(db_session, today, Decimal("10000"))
    await db_session.commit()
    assert state.trades_count == 0

    state = await record_trade_result(db_session, today, Decimal("-150"), max_daily_loss_pct=Decimal("1.0"))
    await db_session.commit()
    assert state.trades_count == 1
    assert state.consecutive_losses == 1
    assert state.is_locked is True  # -150/10000 = 1.5% >= 1.0% limit

    state2 = await get_or_create_daily_risk_state(db_session, today, Decimal("10000"))
    assert state2.trades_count == 1  # re-fetch returns the same row, not a new one


@pytest.mark.asyncio
async def test_paper_order_fill_and_position_lifecycle(db_session):
    order_id = await create_paper_order(
        db_session, signal_id=None, symbol="BTCUSDT", direction="LONG",
        order_type="market", price=None, quantity=Decimal("0.015"),
    )
    await db_session.commit()

    fill_id = await record_fill(
        db_session, order_id=order_id, price=Decimal("68005.0"), quantity=Decimal("0.015"),
        fee=Decimal("0.612"), slippage=Decimal("5"),
    )
    await db_session.commit()
    assert fill_id is not None

    position_id = await open_position(
        db_session, signal_id=None, symbol="BTCUSDT", direction="LONG",
        entry_price=Decimal("68005.0"), quantity=Decimal("0.015"),
        stop_loss=Decimal("67550"), take_profit_1=Decimal("69600"), take_profit_2=Decimal("70800"),
    )
    await db_session.commit()

    from sqlalchemy import select
    from tradingview_mcp.core.database.models.paper_trading import PaperPosition

    position = (
        await db_session.execute(select(PaperPosition).where(PaperPosition.id == position_id))
    ).scalar_one()
    await close_position(db_session, position, realized_pnl=Decimal("24.50"), fees_paid=Decimal("1.22"))
    await db_session.commit()
    assert position.status == "CLOSED"
    assert Decimal(position.realized_pnl) == Decimal("24.50")

    snap_id = await record_equity_snapshot(
        db_session, as_of=_now(), balance=Decimal("10024.50"), equity=Decimal("10024.50"),
        open_positions=0, daily_pnl=Decimal("24.50"),
    )
    await db_session.commit()
    assert snap_id > 0
