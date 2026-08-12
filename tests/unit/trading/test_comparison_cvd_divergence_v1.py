from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_cvd_divergence_v1 import detect_cvd_divergence_setup

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _candle(i: int, high: str, low: str, close: str) -> dict:
    return {
        "open_time": BASE + dt.timedelta(minutes=15 * i),
        "open": Decimal(close),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
    }


def _trade(i: int, delta: str) -> dict:
    return {
        "bucket_start": BASE + dt.timedelta(minutes=15 * i + 1),
        "delta": Decimal(delta),
    }


def _bullish_fixture() -> tuple[list[dict], list[dict]]:
    candles = [
        _candle(0, "101", "100", "100.5"),
        _candle(1, "100.8", "99.5", "100.0"),
        _candle(2, "100.4", "98.8", "99.3"),
        _candle(3, "99.8", "97.5", "98.0"),  # first confirmed swing low
        _candle(4, "99.2", "98.3", "98.9"),
        _candle(5, "100.0", "98.7", "99.5"),
        _candle(6, "99.8", "98.6", "99.0"),
        _candle(7, "99.1", "97.9", "98.4"),
        _candle(8, "98.7", "97.0", "97.6"),  # lower price low
        _candle(9, "98.5", "97.7", "98.2"),
        _candle(10, "99.0", "98.0", "98.7"),
        _candle(11, "99.2", "98.4", "98.9"),
    ]
    # CVD is deeply negative at first low, then recovers so the second lower
    # price low occurs with a higher cumulative delta.
    deltas = ["-5", "-5", "-5", "-20", "12", "10", "8", "4", "2", "1", "1", "1"]
    return candles, [_trade(i, d) for i, d in enumerate(deltas)]


def test_detects_bullish_regular_cvd_divergence_on_dedicated_history() -> None:
    candles, trades = _bullish_fixture()
    chain = {
        "regime": {"primary_regime": "RANGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2.0")}}},
        # Standard W1/W3 history may remain short; the detector must use the
        # separately threaded longer research window instead.
        "_windows": SimpleNamespace(candles_15m=candles, trades_1m=[]),
        "_cvd_trades_1m": trades,
    }
    setup = detect_cvd_divergence_setup(chain)
    assert setup["direction"] == "LONG"
    assert setup["setup_type"] == "cvd_divergence_reversal"
    assert setup["stop_loss"] < setup["entry_zone"]["low"]
    assert len(setup["targets"]) == 3
    assert setup["detection_version"] == "CVD_REGULAR_DIVERGENCE_CONFIRMED_PIVOTS_V2_LONG_HISTORY"


def test_fails_closed_without_dedicated_long_trade_history() -> None:
    candles, trades = _bullish_fixture()
    chain = {
        "regime": {"primary_regime": "RANGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2.0")}}},
        "_windows": SimpleNamespace(candles_15m=candles, trades_1m=trades[-2:]),
    }
    setup = detect_cvd_divergence_setup(chain)
    assert setup["direction"] == "NO_SETUP"
    assert "dedicated 6h" in setup["reasons"][0]


def test_rejects_when_price_and_cvd_both_make_lower_lows() -> None:
    candles, _ = _bullish_fixture()
    trades = [_trade(i, "-5") for i in range(len(candles))]
    chain = {
        "regime": {"primary_regime": "RANGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2.0")}}},
        "_windows": SimpleNamespace(candles_15m=candles, trades_1m=[]),
        "_cvd_trades_1m": trades,
    }
    setup = detect_cvd_divergence_setup(chain)
    assert setup["direction"] == "NO_SETUP"
