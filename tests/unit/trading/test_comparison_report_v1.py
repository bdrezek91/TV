from __future__ import annotations

from tradingview_mcp.core.backtest.comparison_report_v1 import render_markdown, statistical_verdict


def _summary(status="INSUFFICIENT_SAMPLE"):
    return {
        "signals": 10,
        "filled": 5,
        "trades_with_r": 5,
        "sample_status": status,
        "win_rate": "0.6",
        "expectancy_r": "0.2",
        "profit_factor": "1.4",
    }


def _guarded():
    return {
        "status": "COMPLETE_LOCAL_SMOKE",
        "final_statistical_verdict_allowed": False,
        "used_window": {"start": "2026-08-09T00:00:00+00:00", "end": "2026-08-10T00:00:00+00:00"},
        "primary_execution": {
            "summary": {
                "LEGACY_AS_IS": _summary(),
                "LEGACY_FIXED_TV": _summary(),
                "RESEARCH_V2": _summary(),
            }
        },
        "paired_legacy_fixed_vs_research": {"ONLY_RESEARCH_V2": 3, "BOTH_NO_TRADE": 8},
        "research_v2_tv_shadow": {
            "outcomes_by_status": {"CONFIRMED": _summary()},
        },
        "production_as_is_execution_sensitivity": {
            "summary": {
                "LEGACY_AS_IS": _summary(),
                "LEGACY_FIXED_TV": _summary(),
                "RESEARCH_V2": _summary(),
            }
        },
    }


def test_smoke_run_can_never_be_promoted_to_final_winner():
    verdict, reasons = statistical_verdict(_guarded())
    assert verdict == "NO_STATISTICALLY_MEANINGFUL_WINNER"
    assert reasons


def test_report_contains_core_comparison_and_tv_shadow_sections():
    md = render_markdown(_guarded())
    assert "TV / Legacy vs Research V2" in md
    assert "LEGACY_AS_IS" in md
    assert "LEGACY_FIXED_TV" in md
    assert "RESEARCH_V2" in md
    assert "TradingView shadow" in md
    assert "NO_STATISTICALLY_MEANINGFUL_WINNER" in md


def test_degraded_extended_run_is_explicitly_reported_not_promoted():
    extended = {
        "run_kind": "EXTENDED_DEGRADED_RESEARCH_V2_TV_SHADOW",
        "status": "COMPLETE_DEGRADED_REPLAY",
        "final_tv_vs_bot_verdict_allowed": False,
        "limitations": ["DEGRADED_NO_HISTORICAL_ORDERBOOK"],
        "baseline_research_v2": _summary("MODERATE_SAMPLE"),
    }
    md = render_markdown(_guarded(), extended)
    assert "EXTENDED_DEGRADED_RESEARCH_V2_TV_SHADOW" in md
    assert "DEGRADED_NO_HISTORICAL_ORDERBOOK" in md
    assert "Final TV-vs-bot verdict allowed: `False`" in md
