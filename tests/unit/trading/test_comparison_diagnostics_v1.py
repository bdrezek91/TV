from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_diagnostics_v1 import (
    paired_setup_families,
    signal_set_category,
    validate_requested_overlap,
)
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant


UTC = dt.timezone.utc


def _sig(variant: Variant, setup: str, direction: str) -> NeutralSignal:
    return NeutralSignal(
        variant=variant,
        symbol="BTCUSDT",
        decision_time=dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
        setup_name=setup,
        direction=direction,
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("95") if direction == "LONG" else Decimal("106"),
        targets=(Decimal("105"),) if direction == "LONG" else (Decimal("96"),),
    )


def test_signal_set_categories_for_actionable_sets():
    legacy_long = [_sig(Variant.LEGACY_FIXED_TV, "trend_pullback", "LONG")]
    research_long = [_sig(Variant.RESEARCH_V2, "trend_pullback", "LONG")]
    research_short = [_sig(Variant.RESEARCH_V2, "trend_pullback", "SHORT")]
    assert signal_set_category([], []) == "BOTH_NO_TRADE"
    assert signal_set_category(legacy_long, []) == "ONLY_LEGACY"
    assert signal_set_category([], research_long) == "ONLY_RESEARCH_V2"
    assert signal_set_category(legacy_long, research_long) == "BOTH_SIGNAL_SAME_DIRECTION"
    assert signal_set_category(legacy_long, research_short) == "DIRECTION_CONFLICT"


def test_signal_set_with_internal_long_short_mix_is_conflict():
    legacy = [
        _sig(Variant.LEGACY_FIXED_TV, "trend_pullback", "LONG"),
        _sig(Variant.LEGACY_FIXED_TV, "breakout_retest", "SHORT"),
    ]
    research = [_sig(Variant.RESEARCH_V2, "trend_pullback", "LONG")]
    assert signal_set_category(legacy, research) == "DIRECTION_CONFLICT"


def test_matched_setup_families_do_not_force_mean_reversion_equivalent():
    legacy = [_sig(Variant.LEGACY_FIXED_TV, "breakout_retest", "LONG")]
    research = [
        _sig(Variant.RESEARCH_V2, "breakout", "LONG"),
        _sig(Variant.RESEARCH_V2, "mean_reversion", "SHORT"),
    ]
    result = paired_setup_families(legacy, research)
    assert result == {"breakout_family": "BOTH_SIGNAL_SAME_DIRECTION"}
    assert "mean_reversion" not in result


def _coverage():
    return {
        name: {"earliest": "2026-08-01T00:00:00+00:00", "latest": "2026-08-10T12:00:00+00:00"}
        for name in (
            "4h", "1h", "15m", "candles_1m", "trades_1m",
            "liquidations_1m", "orderbook", "derivatives",
        )
    }


def test_requested_window_inside_exact_overlap_passes():
    result = validate_requested_overlap(
        _coverage(),
        dt.datetime(2026, 8, 2, tzinfo=UTC),
        dt.datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert result.complete is True
    assert result.outside_overlap is False
    assert result.missing_sources == ()


def test_missing_required_source_fails_closed():
    coverage = _coverage()
    coverage["liquidations_1m"] = {"earliest": None, "latest": None}
    result = validate_requested_overlap(
        coverage,
        dt.datetime(2026, 8, 2, tzinfo=UTC),
        dt.datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert result.complete is False
    assert result.outside_overlap is True
    assert result.missing_sources == ("liquidations_1m",)


def test_requested_window_outside_overlap_fails():
    result = validate_requested_overlap(
        _coverage(),
        dt.datetime(2026, 7, 31, tzinfo=UTC),
        dt.datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert result.complete is False
    assert result.outside_overlap is True
    assert "exceeds exact overlap" in result.reasons[0]
