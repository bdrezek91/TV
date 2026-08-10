from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_vwap_reversion_v1 import detect_vwap_deviation_reversion_setup

UTC = dt.timezone.utc


def _candle(i: int, o: str, h: str, l: str, c: str, v: str = "10") -> dict:
    return {
        "open_time": dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC) + dt.timedelta(minutes=15 * i),
        "open": Decimal(o), "high": Decimal(h), "low": Decimal(l), "close": Decimal(c), "volume": Decimal(v),
    }


def test_detects_long_below_vwap_with_bullish_rejection() -> None:
    candles = [_candle(i, "100", "101", "99", "100") for i in range(95)]
    candles.append(_candle(95, "97", "98", "95", "97.5"))
    chain = {
        "regime": {"primary_regime": "NO_EDGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=candles),
    }
    setup = detect_vwap_deviation_reversion_setup(chain)
    assert setup["direction"] == "LONG"
    assert setup["stop_loss"] < setup["entry_zone"]["low"]
    assert len(setup["targets"]) == 3


def test_rejects_small_vwap_deviation() -> None:
    candles = [_candle(i, "100", "101", "99", "100") for i in range(96)]
    chain = {
        "regime": {"primary_regime": "NO_EDGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=candles),
    }
    setup = detect_vwap_deviation_reversion_setup(chain)
    assert setup["direction"] == "NO_SETUP"
