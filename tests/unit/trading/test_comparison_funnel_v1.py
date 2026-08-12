from tradingview_mcp.core.backtest.comparison_funnel_v1 import ResearchFunnelDiagnostics


def _chain(*, decision="WAIT_FOR_CONFIRMATION", confirmation="NEUTRAL"):
    risk = {
        "decision": decision,
        "rejection_reasons": [],
        "reasons": ["waiting for stronger independent confirmation"] if decision == "WAIT_FOR_CONFIRMATION" else ["approved"],
    }
    return {
        "symbol": "BTCUSDT",
        "regime": {"primary_regime": "TREND_UP"},
        "data_quality": {
            "data_quality_score": 75,
            "is_tradeable": True,
            "missing_fields": ["orderbook", "liquidations"],
            "stale_fields": [],
        },
        "setups": {
            "trend_pullback": {
                "direction": "LONG",
                "reasons": ["trend candidate"],
            },
            "breakout": {
                "direction": "NO_SETUP",
                "reasons": ["regime TREND_UP does not support a breakout setup"],
            },
            "mean_reversion": {
                "direction": "NO_SETUP",
                "reasons": ["regime TREND_UP does not support a mean-reversion setup"],
            },
            "liquidation_reversal": {
                "direction": "NO_SETUP",
                "reasons": ["regime TREND_UP does not support a liquidation-reversal setup"],
            },
        },
        "confirmations": {
            "trend_pullback": {
                "status": confirmation,
                "available_dimensions": 7,
                "net_independent_score": 0,
                "missing_sources": [
                    "liquidations_longs_shorts",
                    "orderbook_imbalance",
                    "spread_and_depth",
                    "microprice",
                ],
                "used_by_regime": ["cvd"],
                "used_by_setup": ["cvd"],
                "dimensions": [
                    {"dimension": "orderbook_imbalance", "read": "UNAVAILABLE"},
                    {"dimension": "spread_and_depth", "read": "UNAVAILABLE"},
                    {"dimension": "microprice", "read": "UNAVAILABLE"},
                    {"dimension": "liquidations_longs_shorts", "read": "UNAVAILABLE"},
                    {"dimension": "taker_buy_sell_ratio", "read": "NEUTRAL"},
                ],
            }
        },
        "risk_decisions": {"trend_pullback": risk},
    }


def test_funnel_counts_degraded_missing_dimensions_and_waits():
    diag = ResearchFunnelDiagnostics()
    diag.record(_chain())
    out = diag.to_dict()

    assert out["decision_points_total"] == 1
    assert out["w1_regimes"] == {"TREND_UP": 1}
    assert out["w2_active_candidates"] == {"trend_pullback": 1}
    assert out["w2_top_no_setup_reasons"]["breakout:REGIME_INCOMPATIBLE"] == 1
    assert out["w3_statuses"] == {"NEUTRAL": 1}
    assert out["w4_decisions"] == {"WAIT_FOR_CONFIRMATION": 1}
    assert out["degraded_w3_impact"]["missing_orderbook_dimension_events"] == 3
    assert out["degraded_w3_impact"]["missing_liquidation_dimension_events"] == 1
    assert out["derived_rates"]["w2_candidates_per_decision_point"] == 1.0
    assert out["derived_rates"]["final_approved_per_decision_point"] == 0.0


def test_funnel_tracks_approved_confirmation_status():
    diag = ResearchFunnelDiagnostics()
    diag.record(_chain(decision="APPROVED", confirmation="CONFIRMED"))
    out = diag.to_dict()

    assert out["w4_decisions"] == {"APPROVED": 1}
    assert out["w4_approved_by_w3_status"] == {"CONFIRMED": 1}
    assert out["derived_rates"]["w4_approved_per_w2_candidate"] == 1.0
