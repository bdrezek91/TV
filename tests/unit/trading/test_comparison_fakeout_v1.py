from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_fakeout_v1 import detect_failed_breakout_setup

UTC = dt.timezone.utc


def _candle(ts: dt.datetime, *, high: str, low: str, close: str) -> dict:
    return {
        "open_time": ts,
        "open": close,
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
        "volume": Decimal("1"),
    }


def _chain(direction: str | None = None) -> dict:
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    # Prior 20h range is 90..110. Latest closed 1h candle is excluded from
    # that reference by the detector.
    one_h = [
        _candle(start + dt.timedelta(hours=i), high="110", low="90", close="100")
        for i in range(20)
    ]
    one_h.append(_candle(start + dt.timedelta(hours=20), high="111", low="89", close="100"))

    fifteen = [
        _candle(start + dt.timedelta(hours=20, minutes=15 * i), high="105", low="95", close="100")
        for i in range(4)
    ]
    if direction == "SHORT":
        fifteen[-1] = _candle(start + dt.timedelta(hours=20, minutes=45), high="111.5", low="99", close="109")
    elif direction == "LONG":
        fifteen[-1] = _candle(start + dt.timedelta(hours=20, minutes=45), high="101", low="88.5", close="91")

    return {
        "symbol": "TESTUSDT",
        "as_of": start + dt.timedelta(hours=21),
        "regime": {"primary_regime": "RANGE", "confidence": 0.6},
        "features": {"tf": {"1h": {"atr": Decimal("10")}}},
        "_windows": SimpleNamespace(candles_1h=one_h, candles_15m=fifteen),
    }


def test_upper_sweep_reclaim_creates_short() -> None:
    setup = detect_failed_breakout_setup(_chain("SHORT"))
    assert setup["direction"] == "SHORT"
    assert setup["reference_resistance"] == Decimal("110")
    assert setup["stop_loss"] > setup["entry_zone"]["high"]
    assert setup["targets"][0] < setup["entry_zone"]["low"]


def test_lower_sweep_reclaim_creates_long() -> None:
    setup = detect_failed_breakout_setup(_chain("LONG"))
    assert setup["direction"] == "LONG"
    assert setup["reference_support"] == Decimal("90")
    assert setup["stop_loss"] < setup["entry_zone"]["low"]
    assert setup["targets"][0] > setup["entry_zone"]["high"]


def test_no_sweep_is_no_setup() -> None:
    setup = detect_failed_breakout_setup(_chain(None))
    assert setup["direction"] == "NO_SETUP"


def test_trend_regime_is_outside_predeclared_scope() -> None:
    chain = _chain("SHORT")
    chain["regime"]["primary_regime"] = "TREND_UP"
    setup = detect_failed_breakout_setup(chain)
    assert setup["direction"] == "NO_SETUP"
    assert setup["regime_compatible"] is False
