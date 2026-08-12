from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_history_v1 import HistoricalInputs, windows_as_of
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant
from tradingview_mcp.core.backtest.tv_shadow_v1 import TV_SHADOW_VERSION, evaluate_tv_shadow


UTC = dt.timezone.utc


def _candle(ts, price):
    p = Decimal(str(price))
    return {
        "open_time": ts,
        "open": p - Decimal("0.4"),
        "high": p + Decimal("1"),
        "low": p - Decimal("1"),
        "close": p,
        "volume": Decimal("100"),
    }


def _inputs(c1h, c4h):
    return HistoricalInputs(
        candles_4h=c4h, candles_1h=c1h, candles_15m=[], candles_1m=[],
        trades_1m=[], liquidations_1m=[], orderbook=[], derivatives=[],
    )


def _signal(direction="LONG"):
    return NeutralSignal(
        variant=Variant.RESEARCH_V2,
        symbol="BTCUSDT",
        decision_time=dt.datetime(2026, 8, 10, 12, tzinfo=UTC),
        setup_name="trend_pullback",
        direction=direction,
        entry_low=Decimal("150"), entry_high=Decimal("151"),
        stop_loss=Decimal("145") if direction == "LONG" else Decimal("156"),
        targets=(Decimal("160"),) if direction == "LONG" else (Decimal("140"),),
    )


def _rising_history(cutoff):
    c1h = []
    for i in range(70):
        ts = cutoff - dt.timedelta(hours=70 - i)
        c1h.append(_candle(ts, 100 + i * 0.7))
    c4h = []
    for i in range(70):
        ts = cutoff - dt.timedelta(hours=4 * (70 - i))
        c4h.append(_candle(ts, 80 + i * 1.0))
    return c1h, c4h


def test_shadow_can_confirm_aligned_long_but_never_affects_decision():
    signal = _signal("LONG")
    c1h, c4h = _rising_history(signal.decision_time)
    windows = windows_as_of(_inputs(c1h, c4h), signal.decision_time)
    result = evaluate_tv_shadow(signal, windows)
    assert result.status in {"CONFIRMED", "WEAK_CONFIRMATION"}
    assert result.score > 0
    assert result.history_kind == TV_SHADOW_VERSION
    assert result.can_affect_decision is False
    assert result.dimensions["htf_structure"]["overlap_class"] == "ALREADY_USED"


def test_same_bullish_history_contradicts_short_direction():
    signal = _signal("SHORT")
    c1h, c4h = _rising_history(signal.decision_time)
    windows = windows_as_of(_inputs(c1h, c4h), signal.decision_time)
    result = evaluate_tv_shadow(signal, windows)
    assert result.status in {"CONTRADICTED", "NEUTRAL"}
    assert result.score <= 0
    assert result.can_affect_decision is False


def test_future_bar_after_cutoff_cannot_flip_shadow_result():
    signal = _signal("LONG")
    c1h, c4h = _rising_history(signal.decision_time)
    baseline = evaluate_tv_shadow(signal, windows_as_of(_inputs(c1h, c4h), signal.decision_time))

    # Huge future crash starts exactly at cutoff and is not closed/knowable yet.
    c1h_future = c1h + [_candle(signal.decision_time, 1)]
    c4h_future = c4h + [_candle(signal.decision_time, 1)]
    with_future = evaluate_tv_shadow(
        signal, windows_as_of(_inputs(c1h_future, c4h_future), signal.decision_time)
    )
    assert with_future.status == baseline.status
    assert with_future.score == baseline.score
    assert with_future.dimensions == baseline.dimensions


def test_insufficient_history_returns_insufficient_data_not_neutral_confirmation():
    signal = _signal("LONG")
    cutoff = signal.decision_time
    c1h = [_candle(cutoff - dt.timedelta(hours=i + 1), 100) for i in range(10)]
    c4h = [_candle(cutoff - dt.timedelta(hours=4 * (i + 1)), 100) for i in range(10)]
    result = evaluate_tv_shadow(signal, windows_as_of(_inputs(c1h, c4h), cutoff))
    assert result.status == "INSUFFICIENT_DATA"
    assert result.score == Decimal("0")
    assert result.can_affect_decision is False
