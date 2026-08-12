from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_volatility_expansion_v1 import (
    detect_volatility_expansion_setup,
)

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _candle(i: int, o: str, h: str, l: str, c: str) -> dict:
    return {
        "open_time": BASE + dt.timedelta(minutes=15 * i),
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(l),
        "close": Decimal(c),
        "volume": Decimal("10"),
    }


def _chain(candles: list[dict]) -> dict:
    return {
        "regime": {"primary_regime": "RANGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=candles),
    }


def test_detects_breakout_after_equal_window_compression() -> None:
    # First 4h: wide 20-point range. Next 4h: compressed 4-point range.
    older = [_candle(i, "100", "110", "90", "100") for i in range(16)]
    recent = [_candle(16 + i, "100", "102", "98", "100") for i in range(16)]
    trigger = _candle(32, "100", "104", "99", "103")

    setup = detect_volatility_expansion_setup(_chain(older + recent + [trigger]))

    assert setup["direction"] == "LONG"
    assert setup["older_range"] == Decimal("20")
    assert setup["compression_range"] == Decimal("4")
    assert setup["price_only_mode"] is True
    assert setup["detection_version"] == "VOLATILITY_EXPANSION_SQUEEZE_V2_EQUAL_WINDOWS"
    assert setup["stop_loss"] < setup["entry_zone"]["low"]
    assert len(setup["targets"]) == 3


def test_rejects_when_recent_range_is_not_compressed() -> None:
    older = [_candle(i, "100", "110", "90", "100") for i in range(16)]
    recent = [_candle(16 + i, "100", "109", "91", "100") for i in range(16)]
    trigger = _candle(32, "100", "112", "99", "111")

    setup = detect_volatility_expansion_setup(_chain(older + recent + [trigger]))

    assert setup["direction"] == "NO_SETUP"
    assert "compression" in setup["reasons"][0]


def test_requires_two_equal_four_hour_windows_plus_trigger() -> None:
    candles = [_candle(i, "100", "102", "98", "100") for i in range(32)]
    setup = detect_volatility_expansion_setup(_chain(candles))
    assert setup["direction"] == "NO_SETUP"
    assert "equal pre-compression/compression windows" in setup["reasons"][0]


def test_custom_scope_can_enable_trend_regime_without_changing_default() -> None:
    older = [_candle(i, "100", "110", "90", "100") for i in range(16)]
    recent = [_candle(16 + i, "100", "102", "98", "100") for i in range(16)]
    trigger = _candle(32, "100", "104", "99", "103")
    chain = _chain(older + recent + [trigger])
    chain["regime"]["primary_regime"] = "TREND_UP"

    default = detect_volatility_expansion_setup(chain)
    v13_scope = detect_volatility_expansion_setup(
        chain,
        allowed_regimes={"TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN"},
    )

    assert default["direction"] == "NO_SETUP"
    assert v13_scope["direction"] == "LONG"
