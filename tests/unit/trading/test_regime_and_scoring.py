from __future__ import annotations

from decimal import Decimal

import pytest

from tradingview_mcp.core.analysis.regime_classifier import classify_regime
from tradingview_mcp.core.analysis.scoring import (
    StrategyConfigError,
    clear_config_cache,
    load_strategy_config,
    score_setup,
)


def _features(**overrides):
    base = {
        "is_tradeable": True,
        "stale_fields": [],
        "missing_fields": [],
        "price_action": {
            "available": True, "structure": "HH_HL", "ema20": "105", "ema50": "100",
            "ema200": "90", "last_close": "106", "atr_pct": "1.0",
        },
        "orderbook": {"spread_bps": "5"},
    }
    base.update(overrides)
    return base


def test_regime_trend_up():
    result = classify_regime(_features())
    assert result["regime"] == "TREND_UP"
    assert 0 <= result["confidence"] <= 1


def test_regime_trend_down():
    feats = _features(price_action={
        "available": True, "structure": "LH_LL", "ema20": "95", "ema50": "100",
        "ema200": "110", "last_close": "94", "atr_pct": "1.0",
    })
    result = classify_regime(feats)
    assert result["regime"] == "TREND_DOWN"


def test_regime_chaotic_wide_spread():
    feats = _features(orderbook={"spread_bps": "40"})
    result = classify_regime(feats)
    assert result["regime"] == "CHAOTIC"


def test_regime_low_liquidity():
    feats = _features(orderbook={"spread_bps": "20"})
    result = classify_regime(feats)
    assert result["regime"] == "LOW_LIQUIDITY"


def test_regime_high_volatility():
    feats = _features(price_action={
        "available": True, "structure": "UNKNOWN", "ema20": "100", "ema50": "100",
        "ema200": "100", "last_close": "100", "atr_pct": "5.0",
    })
    result = classify_regime(feats)
    assert result["regime"] == "HIGH_VOLATILITY"


def test_regime_range_default():
    feats = _features(price_action={
        "available": True, "structure": "UNKNOWN", "ema20": "100", "ema50": "100",
        "ema200": "100", "last_close": "100", "atr_pct": "1.0",
    })
    result = classify_regime(feats)
    assert result["regime"] == "RANGE"


def test_regime_no_data_when_not_tradeable():
    feats = _features(is_tradeable=False, stale_fields=["trades"])
    result = classify_regime(feats)
    assert result["regime"] == "NO_DATA"


def test_scoring_weights_sum_to_100():
    config = load_strategy_config()
    total = sum(Decimal(str(w)) for w in config["scoring"]["weights"].values())
    assert total == Decimal("100")


def test_scoring_full_marks_when_all_fractions_are_1():
    fractions = {k: Decimal("1") for k in load_strategy_config()["scoring"]["weights"]}
    result = score_setup(fractions)
    assert result.total_score == 100
    assert result.config_version == "v1"


def test_scoring_zero_when_no_fractions_given():
    result = score_setup({})
    assert result.total_score == 0


def test_scoring_partial_components():
    fractions = {"trend_alignment": Decimal("1"), "risk_reward": Decimal("0.5")}
    result = score_setup(fractions)
    # trend_alignment weight 15 * 1.0 + risk_reward weight 10 * 0.5 = 15 + 5 = 20
    assert result.total_score == 20


def test_scoring_rejects_bad_config_weight_sum(tmp_path):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text(
        "version: bad\nscoring:\n  weights:\n    trend_alignment: 50\n    volume: 10\n"
    )
    clear_config_cache()
    with pytest.raises(StrategyConfigError):
        load_strategy_config(str(bad_config))
    clear_config_cache()
