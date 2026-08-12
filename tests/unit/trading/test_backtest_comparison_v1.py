from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from tradingview_mcp.core.backtest.comparison_v1 import (
    Variant,
    bootstrap_mean_ci,
    decision_times,
    legacy_parser_mismatch,
    paired_signal_category,
    parse_current_tv_response_fixed,
    sample_status,
)


def _current_tv_fixture():
    return {
        "mtf": {
            "timeframes": {
                "15m": {"bias": "Bullish"},
                "1h": {"bias": "Bullish"},
                "4h": {"bias": "Bullish"},
            }
        },
        "single": {
            "rsi": {"value": 57.2, "signal": "Neutral"},
            "macd": {"crossover": "Bullish"},
            "ema": {"ema20": 100.5, "ema50": 98.2},
            "bollinger_bands": {"position": "Upper Half"},
            "volume_analysis": {"signal": "Above Average"},
            "support_resistance": {"support_1": 96.0, "resistance_1": 104.0},
        },
    }


def test_legacy_parser_mismatch_reproduces_current_schema_problem():
    mismatch = legacy_parser_mismatch(_current_tv_fixture())
    assert mismatch["mtf_bias_not_consumed_by_legacy_parser"] is True
    assert mismatch["single_has_current_top_level_schema"] is True
    assert mismatch["single_lacks_legacy_indicators_wrapper"] is True


def test_fixed_parser_consumes_bias_and_current_single_shape():
    ctx = parse_current_tv_response_fixed("BTCUSDT", _current_tv_fixture())
    assert ctx.trend_15m == "BULLISH"
    assert ctx.trend_1h == "BULLISH"
    assert ctx.trend_4h == "BULLISH"
    assert ctx.ema20 == Decimal("100.5")
    assert ctx.ema50 == Decimal("98.2")
    assert ctx.rsi == Decimal("57.2")
    assert ctx.macd_signal == "BULLISH"
    assert ctx.volume_confirmation is True
    assert ctx.technical_levels["support_1"] == Decimal("96.0")


def test_decision_clock_matches_warsaw_summer_time_in_utc():
    start = dt.datetime(2026, 8, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 10, 23, 59, tzinfo=dt.timezone.utc)
    rows = decision_times(start, end)
    assert len(rows) == 8
    # Europe/Warsaw is UTC+2 in August: local 07:00 == 05:00 UTC.
    assert rows[0] == dt.datetime(2026, 8, 10, 5, 0, tzinfo=dt.timezone.utc)
    assert rows[-1] == dt.datetime(2026, 8, 10, 19, 0, tzinfo=dt.timezone.utc)


def test_decision_clock_uses_zoneinfo_and_handles_winter_offset():
    start = dt.datetime(2026, 12, 10, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 12, 10, 23, 59, tzinfo=dt.timezone.utc)
    rows = decision_times(start, end, hours=(7,), timezone=ZoneInfo("Europe/Warsaw"))
    # UTC+1 in winter.
    assert rows == [dt.datetime(2026, 12, 10, 6, 0, tzinfo=dt.timezone.utc)]


def test_paired_signal_categories():
    assert paired_signal_category("LONG", "LONG") == "BOTH_SIGNAL_SAME_DIRECTION"
    assert paired_signal_category("SHORT", "SHORT") == "BOTH_SIGNAL_SAME_DIRECTION"
    assert paired_signal_category("LONG", "SHORT") == "DIRECTION_CONFLICT"
    assert paired_signal_category("LONG", None) == "ONLY_LEGACY"
    assert paired_signal_category(None, "SHORT") == "ONLY_RESEARCH_V2"
    assert paired_signal_category(None, None) == "BOTH_NO_TRADE"


def test_sample_status_thresholds():
    assert sample_status(0) == "INSUFFICIENT_SAMPLE"
    assert sample_status(29) == "INSUFFICIENT_SAMPLE"
    assert sample_status(30) == "PRELIMINARY"
    assert sample_status(100) == "MODERATE_SAMPLE"
    assert sample_status(300) == "STRONGER_SAMPLE"


def test_bootstrap_is_deterministic():
    a = bootstrap_mean_ci([Decimal("1"), Decimal("-1"), Decimal("2")], seed=42)
    b = bootstrap_mean_ci([Decimal("1"), Decimal("-1"), Decimal("2")], seed=42)
    assert a == b
    assert a["low"] <= a["mean"] <= a["high"]


def test_variant_names_are_stable_for_report_contract():
    assert Variant.LEGACY_AS_IS.value == "LEGACY_AS_IS"
    assert Variant.LEGACY_FIXED_TV.value == "LEGACY_FIXED_TV"
    assert Variant.RESEARCH_V2.value == "RESEARCH_V2"
    assert Variant.RESEARCH_V2_TV.value == "RESEARCH_V2_TV"
