from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research" / "validate_extended_cache_quality.py"
SPEC = importlib.util.spec_from_file_location("validate_extended_cache_quality", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

UTC = dt.timezone.utc


def test_grid_diagnostics_detects_gap_and_duplicate() -> None:
    t0 = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        {"ts": t0.isoformat()},
        {"ts": (t0 + dt.timedelta(minutes=1)).isoformat()},
        {"ts": (t0 + dt.timedelta(minutes=1)).isoformat()},
        {"ts": (t0 + dt.timedelta(minutes=3)).isoformat()},
    ]
    result = mod._grid_diagnostics(rows, field="ts", seconds=60, name="x")
    assert result["passed"] is False
    assert result["duplicates"] == 1
    assert result["gap_count"] >= 1


def test_funding_sequence_does_not_assume_fixed_interval() -> None:
    t0 = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        {"ts": t0.isoformat()},
        {"ts": (t0 + dt.timedelta(hours=4)).isoformat()},
        {"ts": (t0 + dt.timedelta(hours=12)).isoformat()},
    ]
    result = mod._sequence_diagnostics(rows, field="ts", name="funding")
    assert result["passed"] is True


def test_one_minute_bars_reproduce_fifteen_minute_bar() -> None:
    t0 = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    one = []
    for i in range(15):
        price = 100 + i
        one.append({
            "open_time": (t0 + dt.timedelta(minutes=i)).isoformat(),
            "open": str(price),
            "high": str(price + 2),
            "low": str(price - 1),
            "close": str(price + 1),
            "volume": "2",
        })
    fifteen = [{
        "open_time": t0.isoformat(),
        "open": "100",
        "high": "116",
        "low": "99",
        "close": "115",
        "volume": "30",
    }]
    result = mod._aggregate_1m_to_15m(one, fifteen)
    assert result["passed"] is True
    assert result["compared_complete_15m_bars"] == 1


def test_trade_taker_volume_reconciles_to_kline_volume() -> None:
    t0 = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    candles = [
        {"open_time": t0.isoformat(), "volume": "10"},
        {"open_time": (t0 + dt.timedelta(minutes=1)).isoformat(), "volume": "7"},
    ]
    trades = [
        {"bucket_start": t0.isoformat(), "buy_taker_volume": "6", "sell_taker_volume": "4"},
        {"bucket_start": (t0 + dt.timedelta(minutes=1)).isoformat(), "buy_taker_volume": "2", "sell_taker_volume": "5"},
    ]
    result = mod._trade_volume_reconciliation(candles, trades)
    assert result["passed"] is True
    assert result["mismatch_count"] == 0


def test_trade_volume_mismatch_fails() -> None:
    t0 = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    candles = [{"open_time": t0.isoformat(), "volume": "10"}]
    trades = [{"bucket_start": t0.isoformat(), "buy_taker_volume": "5", "sell_taker_volume": "4"}]
    result = mod._trade_volume_reconciliation(candles, trades)
    assert result["passed"] is False
    assert result["mismatch_count"] == 1
