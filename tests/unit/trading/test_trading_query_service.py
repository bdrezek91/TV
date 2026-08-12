"""Integration tests for the Block 3 query service, against a real local
Postgres (see conftest.db_session — auto-skips if unreachable)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradingview_mcp.core.database.models.paper_trading import PaperEquitySnapshot, PaperPosition
from tradingview_mcp.core.database.models.signals import SignalStatus
from tradingview_mcp.core.database.repositories.signals_repository import save_signal
from tradingview_mcp.core.database.session import get_sessionmaker
from tradingview_mcp.core.services import trading_query_service as svc


def _now():
    return dt.datetime.now(dt.timezone.utc)


async def _seed_signal(session, *, score=82, status=SignalStatus.CANDIDATE, symbol="BTCUSDT"):
    return await save_signal(
        session,
        symbol=symbol, setup_name="trend_pullback", direction="LONG",
        market_regime="TREND_UP", status=status, score=score,
        entry_min=Decimal("68200"), entry_max=Decimal("68400"), stop_loss=Decimal("67550"),
        take_profit_1=Decimal("69600"), take_profit_2=Decimal("70800"),
        risk_reward_1=Decimal("2.0"), risk_reward_2=Decimal("3.6"),
        expires_at=_now() + dt.timedelta(minutes=60), invalidation_reason="15m close below 67550",
        feature_snapshot_id=None, tradingview_context={"trend_4h": "BULLISH"},
        bybit_context={"cvd": "positive"}, risk_context={"risk_amount": "25.00"},
        rejection_reasons=[], strategy_config_version="v1",
        components=[{"name": "trend_alignment", "weight": Decimal("15"), "fraction": Decimal("1"), "contribution": Decimal("15")}],
    )


@pytest.mark.asyncio
async def test_bybit_market_state_no_data_returns_stale(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    result = await svc.bybit_market_state("BTCUSDT")
    assert result["symbol"] == "BTCUSDT"
    assert result["data_quality"] == "STALE_OR_MISSING"
    assert result["regime"]["regime"] == "NO_DATA"


@pytest.mark.asyncio
async def test_rank_futures_opportunities_no_trade_when_empty(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    result = await svc.rank_futures_opportunities(["BTCUSDT"], minimum_score=75)
    assert result["status"] == "NO_TRADE"
    assert result["opportunities"] == []
    assert "reasons" in result


@pytest.mark.asyncio
async def test_rank_futures_opportunities_finds_seeded_signal(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    await _seed_signal(db_session)
    await db_session.commit()

    result = await svc.rank_futures_opportunities(["BTCUSDT"], minimum_score=75)
    assert result["status"] == "OPPORTUNITIES_FOUND"
    assert len(result["opportunities"]) == 1
    assert result["opportunities"][0]["direction"] == "LONG"
    assert result["opportunities"][0]["score"] == 82


@pytest.mark.asyncio
async def test_get_trade_setup_valid_and_invalid_ids(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    sid = await _seed_signal(db_session)
    await db_session.commit()

    ok = await svc.get_trade_setup(str(sid))
    assert ok["signal_id"] == str(sid)
    assert len(ok["score_components"]) == 1

    bad_format = await svc.get_trade_setup("not-a-uuid")
    assert "error" in bad_format

    import uuid
    missing = await svc.get_trade_setup(str(uuid.uuid4()))
    assert "error" in missing


@pytest.mark.asyncio
async def test_paper_portfolio_status_defaults_to_starting_balance(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    result = await svc.paper_portfolio_status()
    assert Decimal(result["balance"]) > 0
    assert result["open_positions"] == []
    assert result["is_locked"] is False


@pytest.mark.asyncio
async def test_paper_portfolio_status_reflects_open_position(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    db_session.add(
        PaperPosition(
            symbol="BTCUSDT", direction="LONG", entry_price="68000", quantity="0.01",
            stop_loss="67500", status="OPEN",
        )
    )
    await db_session.commit()
    result = await svc.paper_portfolio_status()
    assert result["open_positions_count"] == 1


@pytest.mark.asyncio
async def test_strategy_performance_empty_window(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    result = await svc.strategy_performance(days=30)
    assert result["trades"] == 0
    assert result["win_rate"] is None


@pytest.mark.asyncio
async def test_strategy_performance_computes_stats_from_closed_positions(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    now = _now()
    for pnl, direction in [("50.0", "LONG"), ("-20.0", "LONG"), ("30.0", "SHORT")]:
        db_session.add(
            PaperPosition(
                symbol="BTCUSDT", direction=direction, entry_price="68000", quantity="0.01",
                status="CLOSED", realized_pnl=pnl, closed_at=now,
            )
        )
    await db_session.commit()

    result = await svc.strategy_performance(days=30)
    assert result["trades"] == 3
    assert Decimal(result["total_pnl_after_fees"]) == Decimal("60.0")
    assert result["long_vs_short"]["long_trades"] == 2
    assert result["long_vs_short"]["short_trades"] == 1


@pytest.mark.asyncio
async def test_trading_system_health_reports_database_connected(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    result = await svc.trading_system_health()
    assert result["database"]["connected"] is True
    assert "symbols" in result
    assert "paper_broker" in result


@pytest.mark.asyncio
async def test_run_futures_opportunity_scan_no_trade(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    result = await svc.run_futures_opportunity_scan()
    assert result["status"] == "NO_TRADE"
    assert result["market"] == "BYBIT_LINEAR"
    assert "reasons" in result
    assert result["opportunities"] == []


@pytest.mark.asyncio
async def test_run_futures_opportunity_scan_finds_and_caps_at_two(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    await _seed_signal(db_session, score=90)
    await _seed_signal(db_session, score=85, symbol="ETHUSDT")
    await _seed_signal(db_session, score=80, symbol="SOLUSDT")
    await db_session.commit()

    result = await svc.run_futures_opportunity_scan(minimum_score=75, maximum_results=10)
    assert result["status"] == "OPPORTUNITIES_FOUND"
    assert len(result["opportunities"]) <= 2  # capped at 2 regardless of maximum_results


def _patch_session_scope(monkeypatch, db_session):
    """Redirects `trading_query_service`'s `session_scope()` calls to the
    already-open `db_session` fixture, so these tests share one
    transaction/connection instead of opening a second engine against a
    different event loop."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_scope(*args, **kwargs):
        yield db_session

    monkeypatch.setattr(svc, "session_scope", _fake_scope)
