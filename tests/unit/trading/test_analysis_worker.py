"""Integration tests for the previously-unwired `analysis-worker` loop
(see `workers/analysis_worker_main.py`), against a real local Postgres
(see conftest.db_session — auto-skips if unreachable).

Found and fixed live on the user's VPS during Block 1-3 deployment: the
worker was shipped in Block 1/2 as an infra-only stub that never actually
read Bybit data or produced signals. These tests exercise the real
DB-read -> Feature Engine -> Strategy Engine -> DB-write cycle end to end,
using genuine repository writes/reads (not mocked business logic) — only
`fetch_tradingview_context` is stubbed, to keep the suite network-free.
"""
from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import select

from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext
from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.models.analysis import FeatureSnapshot, MarketRegime
from tradingview_mcp.core.database.models.signals import StrategySignal
from tradingview_mcp.core.database.repositories.market_data_repository import (
    insert_orderbook_snapshot,
    insert_trade_aggregate,
    upsert_candle,
    upsert_derivatives_snapshot,
)
from tradingview_mcp.core.observability import metrics as m
from tradingview_mcp.workers import analysis_worker_main as worker


def _patch_session_scope(monkeypatch, db_session):
    @asynccontextmanager
    async def _fake_scope(*args, **kwargs):
        yield db_session

    monkeypatch.setattr(worker, "session_scope", _fake_scope)


async def _seed_market_data(db_session, symbol="BTCUSDT", now=None):
    now = now or dt.datetime.now(dt.timezone.utc)

    # 60 fifteen-minute candles: enough history for EMA50 + the swing-
    # structure detector to have real (non-UNKNOWN) input.
    base = Decimal("67000")
    for i in range(60):
        base += Decimal("15")
        ot = now - dt.timedelta(minutes=15 * (60 - i))
        await upsert_candle(db_session, {
            "symbol": symbol, "interval": "15", "open_time": ot,
            "open": base - 5, "high": base + 10, "low": base - 10, "close": base,
            "volume": "12", "turnover": "800000", "is_closed": True,
            "source": "test", "source_timestamp": ot,
        })

    # A handful of 1m trade-aggregate buckets with positive delta.
    for i in range(10):
        bs = now - dt.timedelta(minutes=10 - i)
        await insert_trade_aggregate(db_session, {
            "symbol": symbol, "bucket_seconds": 60, "bucket_start": bs,
            "buy_taker_volume": "6", "sell_taker_volume": "4", "delta": "2",
            "trade_count": 20, "avg_trade_size": "0.5",
            "largest_trade_size": "1.2", "large_trade_count": 1,
        })

    await insert_orderbook_snapshot(db_session, {
        "symbol": symbol, "best_bid": "68990", "best_ask": "69000",
        "spread": "10", "spread_bps": "1.45", "mid_price": "68995",
        "microprice": "68996", "bid_depth": "12", "ask_depth": "10",
        "imbalance": "0.09", "depth_bands": {}, "is_consistent": True,
        "source_timestamp": now,
    })

    await upsert_derivatives_snapshot(db_session, {
        "symbol": symbol, "open_interest": "125000", "mark_price": "69000",
        "index_price": "68998", "funding_rate": "0.0001", "basis": "2",
        "basis_pct": "0.003", "long_short_ratio": "1.1",
        "source_timestamp": now,
    })
    await db_session.commit()


@pytest.mark.asyncio
async def test_load_inputs_converts_rows_to_expected_dict_shapes(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    await _seed_market_data(db_session)

    inputs = await worker._load_inputs("BTCUSDT", max_ages={})

    assert len(inputs["candles"]) == 60
    assert inputs["candles"][0]["symbol"] == "BTCUSDT"
    assert inputs["candles"][-1]["close"] is not None

    assert len(inputs["trades"]) == 10
    assert inputs["trades"][0]["delta"] is not None

    # depth_bands_json (the ORM column) must be renamed to depth_bands
    # (what compute_orderbook_features reads) — this exact rename was the
    # other easy-to-miss shape mismatch alongside the source_timestamp bug.
    assert "depth_bands" in inputs["orderbook_latest"]
    assert "depth_bands_json" not in inputs["orderbook_latest"]
    assert inputs["orderbook_latest"]["is_consistent"] is True

    assert inputs["derivatives_latest"]["open_interest"] is not None
    assert inputs["open_positions_count"] == 0
    assert inputs["active_keys"] == set()


@pytest.mark.asyncio
async def test_persist_results_writes_snapshot_regime_and_both_signal_statuses(db_session, monkeypatch):
    _patch_session_scope(monkeypatch, db_session)
    now = dt.datetime.now(dt.timezone.utc)

    from tradingview_mcp.core.analysis.strategies.base import StrategySetup
    from tradingview_mcp.core.database.models.signals import SignalStatus
    from tradingview_mcp.core.services.strategy_engine_service import EvaluatedSetup

    approved = EvaluatedSetup(
        setup=StrategySetup(
            setup_name="trend_pullback", direction="LONG",
            entry_min=Decimal("68200"), entry_max=Decimal("68400"), stop_loss=Decimal("67550"),
            take_profit_1=Decimal("69600"), take_profit_2=Decimal("70800"),
            invalidation_reason="15m close below 67550",
        ),
        status=SignalStatus.CANDIDATE, score=82,
        risk_reward_1=Decimal("2.0"), risk_reward_2=Decimal("3.6"),
        reasons=["trend aligned"], warnings=[], components=[],
        risk_result={"approved": True, "position_size": "0.01"},
        regime="TREND_UP", rejection_reasons=[],
    )
    rejected = EvaluatedSetup(
        setup=StrategySetup(
            setup_name="breakout_retest", direction="SHORT",
            entry_min=Decimal("68000"), entry_max=Decimal("68100"), stop_loss=Decimal("68500"),
            take_profit_1=Decimal("67000"), take_profit_2=Decimal("66000"),
            invalidation_reason="reclaim of 68500",
        ),
        status=SignalStatus.REJECTED, score=40,
        risk_reward_1=Decimal("1.0"), risk_reward_2=Decimal("2.0"),
        reasons=[], warnings=[], components=[], risk_result=None,
        regime="TREND_UP", rejection_reasons=["risk_reward below minimum"],
    )
    result = {
        "status": "OPPORTUNITIES_FOUND",
        "regime": {"regime": "TREND_UP", "confidence": 0.8, "reasons": [], "warnings": []},
        "signals": [approved],
        "rejected": [rejected],
    }
    snapshot = {
        "symbol": "BTCUSDT", "as_of": now,
        "price_action": {}, "volume": {}, "orderbook": {}, "futures": {}, "liquidations": {},
        "data_quality_score": 90, "missing_fields": [], "stale_fields": [],
        "source_timestamps": {}, "is_tradeable": True,
    }
    tv_context = TradingViewContext(symbol="BTCUSDT", trend_4h="BULLISH", available=True)

    await worker._persist_results(
        "BTCUSDT", snapshot, result, tv_context, config_version="v1", signal_expiry_minutes=60
    )

    fs = (await db_session.execute(select(FeatureSnapshot).where(FeatureSnapshot.symbol == "BTCUSDT"))).scalars().all()
    assert len(fs) == 1
    assert fs[0].is_tradeable is True

    regimes = (await db_session.execute(select(MarketRegime).where(MarketRegime.symbol == "BTCUSDT"))).scalars().all()
    assert len(regimes) == 1
    assert regimes[0].regime == "TREND_UP"

    signals = (await db_session.execute(select(StrategySignal).where(StrategySignal.symbol == "BTCUSDT"))).scalars().all()
    assert len(signals) == 2
    by_setup = {s.setup_name: s for s in signals}
    assert by_setup["trend_pullback"].status == SignalStatus.CANDIDATE
    assert by_setup["trend_pullback"].score == 82
    assert Decimal(by_setup["trend_pullback"].entry_min) == Decimal("68200")
    assert by_setup["breakout_retest"].status == SignalStatus.REJECTED
    assert by_setup["breakout_retest"].rejection_reasons_json == ["risk_reward below minimum"]
    # feature_snapshot_id links back correctly
    assert by_setup["trend_pullback"].feature_snapshot_id == fs[0].id


@pytest.mark.asyncio
async def test_evaluate_symbol_cycle_runs_end_to_end_without_crashing(db_session, monkeypatch):
    """Full DB-round-trip smoke test with real seeded data. Doesn't assert
    a specific strategy fires (that's covered by the strategies' own unit
    tests) — asserts the new glue code runs the whole cycle against a real
    database without raising, and always persists a snapshot + regime."""
    _patch_session_scope(monkeypatch, db_session)
    now = dt.datetime.now(dt.timezone.utc)
    await _seed_market_data(db_session, now=now)

    async def _fake_fetch_context(symbol, exchange="BYBIT", **kwargs):
        return TradingViewContext(symbol=symbol, trend_4h="BULLISH", trend_1h="BULLISH", available=True)

    monkeypatch.setattr(worker, "fetch_tradingview_context", _fake_fetch_context)

    settings = get_trading_settings()
    before_errors = m.collector_errors_total.labels(component="analysis_worker")._value.get()

    await worker._evaluate_symbol_cycle(settings, "BTCUSDT")

    after_errors = m.collector_errors_total.labels(component="analysis_worker")._value.get()
    assert after_errors == before_errors  # no exception was swallowed into an error metric

    fs = (await db_session.execute(select(FeatureSnapshot).where(FeatureSnapshot.symbol == "BTCUSDT"))).scalars().all()
    assert len(fs) == 1

    regimes = (await db_session.execute(select(MarketRegime).where(MarketRegime.symbol == "BTCUSDT"))).scalars().all()
    assert len(regimes) == 1
    assert regimes[0].regime in (
        "TREND_UP", "TREND_DOWN", "RANGE", "BREAKOUT_ATTEMPT",
        "HIGH_VOLATILITY", "LOW_VOLATILITY", "LOW_LIQUIDITY", "CHAOTIC", "NO_DATA",
    )
