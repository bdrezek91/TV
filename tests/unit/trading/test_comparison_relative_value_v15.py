from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest

from tradingview_mcp.core.backtest.comparison_relative_value_v15 import (
    ENTRY_Z,
    detect_v15_pair_signal,
    simulate_v15_pair,
    validate_v15_development_cache_root,
)

UTC = dt.timezone.utc


def _hourly_prices(start: dt.datetime) -> tuple[list[dict], list[dict], dt.datetime]:
    btc_returns = [0.01 if index % 2 == 0 else -0.008 for index in range(72)]
    alt_returns = [1.2 * value for value in btc_returns]
    alt_returns[-1] += 0.20
    btc_prices, alt_prices = [100.0], [50.0]
    for alt_return, btc_return in zip(alt_returns, btc_returns):
        btc_prices.append(btc_prices[-1] * math.exp(btc_return))
        alt_prices.append(alt_prices[-1] * math.exp(alt_return))

    def candles(prices: list[float]) -> list[dict]:
        return [
            {
                "open_time": start + dt.timedelta(hours=index),
                "open": str(price),
                "high": str(price),
                "low": str(price),
                "close": str(price),
            }
            for index, price in enumerate(prices)
        ]

    cutoff = start + dt.timedelta(hours=73)
    return candles(alt_prices), candles(btc_prices), cutoff


def test_v15_detector_uses_only_closed_aligned_history() -> None:
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    alt, btc, cutoff = _hourly_prices(start)
    result = detect_v15_pair_signal("ETHUSDT", alt, btc, cutoff)

    assert result["eligible"] is True
    assert result["pair_direction"] == "SHORT_ALT_LONG_BTC"
    assert result["zscore"] >= float(ENTRY_Z)
    assert result["future_data_used"] is False

    future = {
        "open_time": cutoff,
        "open": "1",
        "high": "999999",
        "low": "0.1",
        "close": "999999",
    }
    assert (
        detect_v15_pair_signal("ETHUSDT", alt + [future], btc + [future], cutoff)
        == result
    )


def test_v15_pair_simulator_charges_both_legs_and_exits_on_reversion() -> None:
    decision = dt.datetime(2026, 1, 1, tzinfo=UTC)
    signal = {
        "eligible": True,
        "decision_time": decision,
        "alt_symbol": "ETHUSDT",
        "pair_direction": "SHORT_ALT_LONG_BTC",
        "alt_direction": "SHORT",
        "btc_direction": "LONG",
        "beta": 1.0,
        "spread_mean": 0.0,
        "spread_std": math.log(1.2) / 2.5,
    }
    alt = [
        {"open_time": decision, "open": "120"},
        {"open_time": decision + dt.timedelta(minutes=1), "open": "100"},
    ]
    btc = [
        {"open_time": decision, "open": "100"},
        {"open_time": decision + dt.timedelta(minutes=1), "open": "100"},
    ]

    result = simulate_v15_pair(signal, alt, btc)

    assert result.exit_reason == "RESIDUAL_REVERTED"
    assert result.observation.filled is True
    assert result.gross_pnl > 0
    assert result.net_pnl < result.gross_pnl
    assert result.fees > 0
    assert result.slippage > 0
    assert result.observation.r_multiple is not None
    assert result.observation.r_multiple > 0


def test_v15_pair_simulator_fails_closed_without_synchronized_entry() -> None:
    decision = dt.datetime(2026, 1, 1, tzinfo=UTC)
    signal = {
        "eligible": True,
        "decision_time": decision,
        "alt_symbol": "ETHUSDT",
        "pair_direction": "LONG_ALT_SHORT_BTC",
        "alt_direction": "LONG",
        "btc_direction": "SHORT",
        "beta": 1.0,
        "spread_mean": 0.0,
        "spread_std": 1.0,
    }

    result = simulate_v15_pair(signal, [], [])

    assert result.exit_reason == "NO_SYNCHRONIZED_ENTRY"
    assert result.observation.filled is False
    assert result.observation.r_multiple is None


def test_v15_development_rejects_reserved_mr_v2_holdout(tmp_path: Path) -> None:
    reserved = tmp_path / "holdout_mrv2_120d" / "cache"
    with pytest.raises(ValueError, match="reserved holdout"):
        validate_v15_development_cache_root(reserved)
