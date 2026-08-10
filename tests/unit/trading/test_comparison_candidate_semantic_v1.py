import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_candidate_semantic_v1 import (
    repair_setup_geometry_preserve_targets,
    weighted_target_profile_rr,
)

UTC = dt.timezone.utc


def _features():
    return {"tf": {"1h": {"available": True, "atr": Decimal("10"), "support": Decimal("90"), "resistance": Decimal("110")}}}


def test_semantic_repair_keeps_original_mean_reversion_targets():
    setup = {
        "setup_type": "mean_reversion",
        "direction": "LONG",
        "entry_zone": {"low": Decimal("90"), "high": Decimal("100")},
        "invalidation": Decimal("85"),
        "stop_loss": Decimal("85"),
        "targets": [Decimal("105"), Decimal("120")],
        "reasons": [],
    }
    out = repair_setup_geometry_preserve_targets(setup, _features())
    assert out["targets"] == [Decimal("105"), Decimal("120")]
    assert "SEMANTIC_TARGETS_PRESERVED" in out["comparison_candidate_adjustments"]
    assert "TARGETS_ALIGNED_TO_W4_WORST_ENTRY_R_MULTIPLES" not in out["comparison_candidate_adjustments"]


def test_weighted_profile_uses_normalized_two_target_weights():
    setup = {
        "direction": "LONG",
        "entry_zone": {"low": Decimal("100"), "high": Decimal("100")},
        "stop_loss": Decimal("90"),
        "targets": [Decimal("110"), Decimal("130")],
    }
    profile = weighted_target_profile_rr(setup)
    assert profile["valid"] is True
    # 50/30 normalized -> 62.5% / 37.5%; 1R and 3R -> 1.75R weighted.
    assert profile["weights"] == [Decimal("0.625"), Decimal("0.375")]
    assert profile["weighted_r"] == Decimal("1.750")


def test_weighted_profile_rejects_target_on_wrong_side():
    setup = {
        "direction": "SHORT",
        "entry_zone": {"low": Decimal("100"), "high": Decimal("100")},
        "stop_loss": Decimal("110"),
        "targets": [Decimal("105"), Decimal("80")],
    }
    profile = weighted_target_profile_rr(setup)
    assert profile["valid"] is False
    assert profile["target_r"][0] < 0


def test_three_target_weighted_profile_matches_50_30_20():
    setup = {
        "direction": "LONG",
        "entry_zone": {"low": Decimal("100"), "high": Decimal("100")},
        "stop_loss": Decimal("90"),
        "targets": [Decimal("110"), Decimal("120"), Decimal("140")],
    }
    profile = weighted_target_profile_rr(setup)
    assert profile["weights"] == [Decimal("0.50"), Decimal("0.30"), Decimal("0.20")]
    assert profile["weighted_r"] == Decimal("1.900")
