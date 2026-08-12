from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingview_mcp.core.backtest import comparison_breakout_v14 as v14
from tradingview_mcp.core.backtest.comparison_breakout_v14 import (
    V14_NAME,
    detect_v14_setup,
    evaluate_breakout_v14,
    validate_v14_development_cache_root,
)


def _chain(*, regime: str = "TREND_UP", trigger_close: str = "105") -> dict:
    trigger_close_value = Decimal(trigger_close)
    history = [
        {"open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100")}
        for _ in range(16)
    ]
    trigger = {
        "open": Decimal("100"),
        "high": max(Decimal("100"), trigger_close_value),
        "low": min(Decimal("99.5"), trigger_close_value),
        "close": trigger_close_value,
    }
    return {
        "regime": {"primary_regime": regime},
        "features": {"tf": {"1h": {"atr": Decimal("10")}}},
        "_windows": SimpleNamespace(candles_15m=history + [trigger]),
    }


def test_detects_fixed_channel_breakout_in_regime_direction() -> None:
    setup = detect_v14_setup(_chain())
    assert setup["direction"] == "LONG"
    assert setup["detection_version"] == V14_NAME
    assert setup["regime_compatible"] is True


def test_rejects_breakout_against_directional_regime() -> None:
    setup = detect_v14_setup(_chain(regime="TREND_DOWN", trigger_close="105"))
    assert setup["direction"] == "NO_SETUP"


def test_rejects_non_directional_regime() -> None:
    setup = detect_v14_setup(_chain(regime="RANGE"))
    assert setup["direction"] == "NO_SETUP"


def test_detects_short_breakout_and_builds_ordered_risk_geometry() -> None:
    chain = _chain(regime="TREND_DOWN", trigger_close="95")
    trigger = chain["_windows"].candles_15m[-1]
    assert trigger["high"] >= max(trigger["open"], trigger["close"])
    assert trigger["low"] <= min(trigger["open"], trigger["close"])

    setup = detect_v14_setup(chain)
    assert setup["direction"] == "SHORT"
    assert setup["entry_zone"]["low"] < setup["entry_zone"]["high"]
    assert setup["stop_loss"] > setup["entry_zone"]["high"]
    assert setup["targets"] == sorted(setup["targets"], reverse=True)


def test_rejects_breakout_trigger_with_body_below_frozen_minimum() -> None:
    chain = _chain()
    chain["_windows"].candles_15m[-1] = {
        "open": Decimal("101.4"),
        "high": Decimal("102"),
        "low": Decimal("101.3"),
        "close": Decimal("101.6"),
    }
    setup = detect_v14_setup(chain)
    assert setup["direction"] == "NO_SETUP"
    assert setup["reasons"] == ["trigger body too small"]


def test_approved_v14_signal_preserves_existing_w3_w4_decisions(monkeypatch) -> None:
    chain = _chain()
    chain.update({"symbol": "BTCUSDT", "as_of": dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), "data_quality": {}})
    chain["features"].update({
        "volume": {
            "available": True,
            "cvd_slope_15m": Decimal("2"),
            "buy_taker_volume": Decimal("60"),
            "sell_taker_volume": Decimal("40"),
        },
        "futures": {
            "available": True,
            "price_to_oi_relation": "PRICE_UP_OI_UP",
            "funding_rate": Decimal("0"),
        },
        "orderbook": {},
        "liquidations": {},
    })
    monkeypatch.setattr(v14.ofc, "confirm_setup", lambda *args, **kwargs: {"status": "CONFIRMED"})
    monkeypatch.setattr(v14.rm, "evaluate_risk", lambda *args, **kwargs: {"decision": "APPROVED"})

    result = evaluate_breakout_v14(chain)

    assert result["gates"]["eligible"] is True
    assert result["signal"] is not None
    assert result["signal"].metadata["detection_version"] == V14_NAME


def test_development_runner_rejects_reserved_holdout_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved 120d holdout"):
        validate_v14_development_cache_root(tmp_path / "holdout_mrv2_120d" / "cache")


def test_development_runner_accepts_revealed_data_path(tmp_path: Path) -> None:
    path = tmp_path / "revealed_90d" / "cache"
    assert validate_v14_development_cache_root(path) == path.resolve()
