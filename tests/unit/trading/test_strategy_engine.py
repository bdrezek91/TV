from __future__ import annotations

from decimal import Decimal

from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext
from tradingview_mcp.core.database.models.signals import SignalStatus
from tradingview_mcp.core.services.strategy_engine_service import (
    DailyState,
    DuplicateChecker,
    evaluate_symbol,
)


def _bullish_features():
    return {
        "is_tradeable": True,
        "stale_fields": [],
        "missing_fields": [],
        "price_action": {
            "available": True, "structure": "HH_HL",
            "ema20": "105", "ema50": "100", "ema200": "90",
            "last_close": "106", "atr": "2", "atr_pct": "1.0",
            "support": "95", "resistance": "115",
        },
        "volume": {"delta": "5"},
        "orderbook": {"spread_bps": "5", "is_consistent": True},
        "futures": {"price_to_oi_relation": None},
        "liquidations": {"available": False},
    }


def _bearish_features():
    return {
        "is_tradeable": True,
        "stale_fields": [],
        "missing_fields": [],
        "price_action": {
            "available": True, "structure": "LH_LL",
            "ema20": "95", "ema50": "100", "ema200": "110",
            "last_close": "94", "atr": "2", "atr_pct": "1.0",
            "support": "85", "resistance": "105",
        },
        "volume": {"delta": "-5"},
        "orderbook": {"spread_bps": "5", "is_consistent": True},
        "futures": {"price_to_oi_relation": None},
        "liquidations": {"available": False},
    }


def _flat_tv(trend="BULLISH"):
    return TradingViewContext(symbol="BTCUSDT", trend_15m=trend, trend_1h=trend, trend_4h=trend, available=True)


def _risk_kwargs(**overrides):
    base = dict(
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.25"),
        tick_size=Decimal("0.5"),
        qty_step=Decimal("0.001"),
        fee_rate=Decimal("0.0006"),
        slippage=Decimal("0.5"),
        max_leverage=Decimal("5"),
    )
    base.update(overrides)
    return base


def test_evaluate_symbol_produces_approved_long_setup():
    result = evaluate_symbol(
        "BTCUSDT", _bullish_features(), _flat_tv("BULLISH"), **_risk_kwargs()
    )
    assert result["status"] == "OPPORTUNITIES_FOUND"
    assert len(result["signals"]) >= 1
    long_signal = next(s for s in result["signals"] if s.setup.direction == "LONG")
    assert long_signal.status == SignalStatus.CANDIDATE
    assert long_signal.setup.setup_name == "trend_pullback"
    assert long_signal.risk_result["approved"] is True
    assert long_signal.risk_reward_1 >= Decimal("2.0")
    assert long_signal.score is not None and long_signal.score > 0


def test_evaluate_symbol_produces_approved_short_setup():
    result = evaluate_symbol(
        "BTCUSDT", _bearish_features(), _flat_tv("BEARISH"), **_risk_kwargs()
    )
    assert result["status"] == "OPPORTUNITIES_FOUND"
    short_signal = next(s for s in result["signals"] if s.setup.direction == "SHORT")
    assert short_signal.setup.stop_loss > short_signal.setup.entry_reference
    assert short_signal.setup.take_profit_1 < short_signal.setup.entry_reference


def test_evaluate_symbol_returns_no_trade_when_trends_disagree():
    features = _bullish_features()
    tv = TradingViewContext(symbol="BTCUSDT", trend_4h="BULLISH", trend_1h="BEARISH", available=True)
    result = evaluate_symbol("BTCUSDT", features, tv, **_risk_kwargs())
    assert result["status"] == "NO_TRADE"
    assert "reasons" in result


def test_evaluate_symbol_rejects_on_stale_data():
    features = _bullish_features()
    features["is_tradeable"] = False
    result = evaluate_symbol("BTCUSDT", features, _flat_tv("BULLISH"), **_risk_kwargs())
    assert result["status"] == "NO_TRADE"
    assert any(r.status == SignalStatus.REJECTED for r in result["rejected"])
    assert any("stale" in reason for r in result["rejected"] for reason in r.rejection_reasons)


def test_evaluate_symbol_rejects_active_duplicate():
    dup = DuplicateChecker(has_duplicate=lambda symbol, setup, direction: True)
    result = evaluate_symbol(
        "BTCUSDT", _bullish_features(), _flat_tv("BULLISH"), duplicate_checker=dup, **_risk_kwargs()
    )
    assert result["status"] == "NO_TRADE"
    assert len(result["rejected"]) >= 1
    assert result["rejected"][0].status == SignalStatus.REJECTED


def test_evaluate_symbol_rejects_when_daily_trade_limit_hit():
    daily = DailyState(daily_trades_count=5, max_trades_per_day=5)
    result = evaluate_symbol(
        "BTCUSDT", _bullish_features(), _flat_tv("BULLISH"), daily_state=daily, **_risk_kwargs()
    )
    assert result["status"] == "NO_TRADE"


def test_evaluate_symbol_rejects_when_consecutive_losses_hit():
    daily = DailyState(consecutive_losses=3, max_consecutive_losses=3)
    result = evaluate_symbol(
        "BTCUSDT", _bullish_features(), _flat_tv("BULLISH"), daily_state=daily, **_risk_kwargs()
    )
    assert result["status"] == "NO_TRADE"


def test_liquidation_reversal_never_produces_a_signal_when_disabled():
    result = evaluate_symbol(
        "BTCUSDT", _bullish_features(), _flat_tv("BULLISH"),
        enable_liquidation_reversal=False, **_risk_kwargs()
    )
    assert all(s.setup.setup_name != "liquidation_exhaustion_reversal" for s in result["signals"])


def test_liquidation_reversal_stays_experimental_and_rejected_even_when_enabled():
    features = _bullish_features()
    features["liquidations"] = {
        "available": True, "zscore_1m": Decimal("3.0"), "cascade_detected": True,
        "long_liq_value_15m": Decimal("200000"), "short_liq_value_15m": Decimal("0"),
    }
    features["orderbook"]["absorption_detected"] = True
    result = evaluate_symbol(
        "BTCUSDT", features, _flat_tv("BULLISH"), enable_liquidation_reversal=True, **_risk_kwargs()
    )
    liq_signals = [s for s in result["signals"] if s.setup.setup_name == "liquidation_exhaustion_reversal"]
    assert liq_signals == []  # never approved into `signals`, regardless of enable flag
    liq_rejected = [r for r in result["rejected"] if r.setup.setup_name == "liquidation_exhaustion_reversal"]
    assert len(liq_rejected) == 1
    assert liq_rejected[0].setup.experimental is True
