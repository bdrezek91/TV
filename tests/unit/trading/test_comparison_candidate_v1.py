from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_candidate_v1 import (
    classification_data_quality_for_degraded_replay,
    repair_setup_geometry,
)


def _features(*, atr="10", support="90", resistance="110"):
    return {
        "tf": {
            "1h": {
                "available": True,
                "atr": Decimal(atr),
                "support": Decimal(support),
                "resistance": Decimal(resistance),
            }
        }
    }


def _rr(setup):
    z = setup["entry_zone"]
    low, high = Decimal(str(z["low"])), Decimal(str(z["high"]))
    stop = Decimal(str(setup["stop_loss"]))
    target = Decimal(str(setup["targets"][0]))
    worst = high if setup["direction"] == "LONG" else low
    return abs(target - worst) / abs(worst - stop)


def test_degraded_w1_debiases_only_orderbook_missing():
    dq = {
        "data_quality_score": 75,
        "is_tradeable": True,
        "missing_fields": ["orderbook"],
        "stale_fields": [],
    }
    out = classification_data_quality_for_degraded_replay(dq)
    assert out["data_quality_score"] == 100
    assert out["comparison_confidence_score_original"] == 75
    assert dq["data_quality_score"] == 75


def test_degraded_w1_does_not_hide_other_missing_sources():
    dq = {
        "data_quality_score": 60,
        "is_tradeable": True,
        "missing_fields": ["orderbook", "candles_1h"],
        "stale_fields": [],
    }
    out = classification_data_quality_for_degraded_replay(dq)
    assert out["data_quality_score"] == 60
    assert "comparison_confidence_adjustment" not in out


def test_invalid_long_stop_is_moved_outside_entry_zone_and_rr_contract_aligned():
    setup = {
        "setup_type": "trend_pullback",
        "direction": "LONG",
        "entry_zone": {"low": Decimal("100"), "high": Decimal("105")},
        "invalidation": Decimal("103"),
        "stop_loss": Decimal("98"),
        "targets": [Decimal("110"), Decimal("115"), Decimal("120")],
        "reasons": [],
    }
    # Already outside the zone; target geometry is what needs alignment.
    out = repair_setup_geometry(setup, _features())
    assert Decimal(str(out["stop_loss"])) < Decimal("100")
    assert _rr(out) >= Decimal("1.5")
    assert "TARGETS_ALIGNED_TO_W4_WORST_ENTRY_R_MULTIPLES" in out["comparison_candidate_adjustments"]


def test_stop_inside_zone_is_repaired():
    setup = {
        "setup_type": "trend_pullback",
        "direction": "LONG",
        "entry_zone": {"low": Decimal("100"), "high": Decimal("105")},
        "invalidation": Decimal("102"),
        "stop_loss": Decimal("102"),
        "targets": [Decimal("120"), Decimal("130"), Decimal("140")],
        "reasons": [],
    }
    out = repair_setup_geometry(setup, _features())
    assert Decimal(str(out["stop_loss"])) < Decimal("100")
    assert "SL_MOVED_OUTSIDE_ENTRY_ZONE" in out["comparison_candidate_adjustments"]


def test_breakout_stop_is_anchored_to_broken_level_not_opposite_range_edge():
    setup = {
        "setup_type": "breakout",
        "direction": "LONG",
        "entry_zone": {"low": Decimal("111"), "high": Decimal("117")},
        "invalidation": Decimal("89"),
        "stop_loss": Decimal("84"),
        "targets": [Decimal("120"), Decimal("130"), Decimal("140")],
        "reasons": [],
    }
    out = repair_setup_geometry(setup, _features(atr="10", support="90", resistance="110"))
    stop = Decimal(str(out["stop_loss"]))
    assert stop == Decimal("105")
    assert out["invalidation"] == Decimal("110")
    assert "BREAKOUT_STOP_ANCHORED_TO_BROKEN_LEVEL" in out["comparison_candidate_adjustments"]
    assert _rr(out) >= Decimal("1.5")


def test_short_breakout_geometry_is_symmetric():
    setup = {
        "setup_type": "breakout",
        "direction": "SHORT",
        "entry_zone": {"low": Decimal("83"), "high": Decimal("89")},
        "invalidation": Decimal("111"),
        "stop_loss": Decimal("116"),
        "targets": [Decimal("80"), Decimal("70"), Decimal("60")],
        "reasons": [],
    }
    out = repair_setup_geometry(setup, _features(atr="10", support="90", resistance="110"))
    stop = Decimal(str(out["stop_loss"]))
    assert stop == Decimal("95")
    assert out["invalidation"] == Decimal("90")
    assert stop > Decimal("89")
    assert _rr(out) >= Decimal("1.5")
