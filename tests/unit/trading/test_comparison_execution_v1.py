from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_execution_v1 import (
    EntryExitIntrabarPolicy,
    TargetSchedulePolicy,
    build_execution_config,
    production_two_target_residual_fraction,
    simulate_neutral_signal,
)
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant


UTC = dt.timezone.utc


def _signal(*, targets=(Decimal("105"), Decimal("110"))) -> NeutralSignal:
    return NeutralSignal(
        variant=Variant.RESEARCH_V2,
        symbol="BTCUSDT",
        decision_time=dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
        setup_name="test_setup",
        direction="LONG",
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("95"),
        targets=tuple(targets),
        metadata={},
    )


def _candle(minute: int, *, low: str, high: str, open_: str = "101", close: str = "101") -> dict:
    return {
        "open_time": dt.datetime(2026, 8, 10, 10, minute, tzinfo=UTC),
        "open": Decimal(open_),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
    }


def test_two_target_production_schedule_leaves_twenty_percent_residual():
    assert production_two_target_residual_fraction() == Decimal("0.20")


def test_normalized_two_target_schedule_sums_to_one():
    cfg = build_execution_config(2, target_schedule_policy=TargetSchedulePolicy.NORMALIZE_AVAILABLE_TARGETS)
    assert cfg["tp1_close_pct"] + cfg["tp2_close_pct"] == Decimal("1")
    # Preserve the production 50:30 relative weighting rather than inventing 50:50.
    assert cfg["tp1_close_pct"] == Decimal("0.625")
    assert cfg["tp2_close_pct"] == Decimal("0.375")


def test_primary_policy_does_not_exit_on_same_candle_as_entry():
    signal = _signal(targets=(Decimal("105"),))
    # This one OHLC candle touches both the entry zone and TP1. Historical
    # OHLC cannot tell whether TP was before or after the fill.
    candles = [_candle(0, low="99", high="106")]

    conservative = simulate_neutral_signal(
        signal,
        candles,
        intrabar_policy=EntryExitIntrabarPolicy.NEXT_CANDLE,
    )
    production_like = simulate_neutral_signal(
        signal,
        candles,
        intrabar_policy=EntryExitIntrabarPolicy.PRODUCTION_SAME_WINDOW,
    )

    assert conservative.order["status"] == "OPEN"
    assert production_like.order["status"] == "CLOSED"
    assert production_like.order["exits"][0]["level"] == "TP1"


def test_normalized_two_target_execution_can_fully_close_position():
    signal = _signal()
    candles = [
        _candle(0, low="99", high="102"),             # entry fill only
        _candle(1, low="100.7", high="105.5"),       # TP1
        _candle(2, low="100.7", high="111"),         # TP2, no BE stop touch
    ]
    result = simulate_neutral_signal(
        signal,
        candles,
        target_schedule_policy=TargetSchedulePolicy.NORMALIZE_AVAILABLE_TARGETS,
        intrabar_policy=EntryExitIntrabarPolicy.NEXT_CANDLE,
    )
    assert result.order["status"] == "CLOSED"
    assert result.order["remaining_size"] == Decimal("0")
    assert [e["level"] for e in result.order["exits"]] == ["TP1", "TP2"]


def test_comparison_reports_entry_fee_missing_from_production_realized_pnl():
    signal = _signal(targets=(Decimal("105"),))
    candles = [
        _candle(0, low="99", high="102"),
        _candle(1, low="101", high="106"),
    ]
    result = simulate_neutral_signal(signal, candles)
    assert result.order["status"] == "CLOSED"
    assert result.entry_fee_omitted_by_production_pnl > 0
    assert result.corrected_net_pnl == (
        result.production_pnl_as_is - result.entry_fee_omitted_by_production_pnl
    )
    assert result.r_multiple_corrected < result.r_multiple_production


def test_candles_before_decision_are_never_used_for_fill():
    signal = _signal(targets=(Decimal("105"),))
    before = {
        "open_time": dt.datetime(2026, 8, 10, 9, 59, tzinfo=UTC),
        "open": Decimal("101"), "high": Decimal("102"), "low": Decimal("99"), "close": Decimal("101"),
    }
    after_no_touch = _candle(0, low="102", high="103")
    result = simulate_neutral_signal(signal, [before, after_no_touch])
    assert result.order["entry_time"] is None
    assert result.order["status"] == "PENDING_ENTRY"
