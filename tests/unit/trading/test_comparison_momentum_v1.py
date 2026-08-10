from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_momentum_v1 import detect_momentum_continuation_setup

UTC = dt.timezone.utc


def _candle(i: int, o: str, h: str, l: str, c: str) -> dict:
    return {
        "open_time": dt.datetime(2026, 8, 1, 10, 0, tzinfo=UTC) + dt.timedelta(minutes=15 * i),
        "open": Decimal(o), "high": Decimal(h), "low": Decimal(l), "close": Decimal(c),
    }


def test_detects_long_impulse_hold_without_future_data() -> None:
    candles = [
        _candle(0, "100", "100.5", "99.8", "100.4"),
        _candle(1, "100.4", "101.0", "100.2", "100.9"),
        _candle(2, "100.9", "101.5", "100.8", "101.4"),
        _candle(3, "101.4", "102.1", "101.3", "102.0"),
    ]
    chain = {
        "regime": {"primary_regime": "NO_EDGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2.0")}}},
        "_windows": SimpleNamespace(candles_15m=candles),
    }
    setup = detect_momentum_continuation_setup(chain)
    assert setup["direction"] == "LONG"
    assert setup["setup_type"] == "momentum_continuation"
    assert setup["stop_loss"] < setup["entry_zone"]["low"]
    assert len(setup["targets"]) == 3


def test_rejects_weak_one_hour_move() -> None:
    candles = [
        _candle(0, "100", "100.2", "99.9", "100.1"),
        _candle(1, "100.1", "100.3", "100.0", "100.2"),
        _candle(2, "100.2", "100.4", "100.1", "100.3"),
        _candle(3, "100.3", "100.5", "100.2", "100.4"),
    ]
    chain = {
        "regime": {"primary_regime": "NO_EDGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2.0")}}},
        "_windows": SimpleNamespace(candles_15m=candles),
    }
    setup = detect_momentum_continuation_setup(chain)
    assert setup["direction"] == "NO_SETUP"
