from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import HistoricalInputs, windows_as_of


UTC = dt.timezone.utc


def _candle(ts, price):
    p = Decimal(str(price))
    return {"open_time": ts, "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": Decimal("1")}


def _trade(ts, delta):
    d = Decimal(str(delta))
    return {
        "bucket_start": ts,
        "buy_taker_volume": max(d, Decimal("0")) + Decimal("1"),
        "sell_taker_volume": max(-d, Decimal("0")) + Decimal("1"),
        "delta": d, "trade_count": 2, "avg_trade_size": Decimal("1"),
        "large_trade_count": 0, "largest_trade_size": Decimal("1"),
    }


def test_indexed_windows_match_reference_windows_for_decision_sources():
    cutoff = dt.datetime(2026, 8, 10, 12, tzinfo=UTC)
    c4 = [_candle(cutoff - dt.timedelta(hours=4 * (i + 1)), 100 + i) for i in reversed(range(60))]
    c1 = [_candle(cutoff - dt.timedelta(hours=i + 1), 100 + i) for i in reversed(range(80))]
    c15 = [_candle(cutoff - dt.timedelta(minutes=15 * (i + 1)), 100 + i) for i in reversed(range(150))]
    c1m = [_candle(cutoff - dt.timedelta(minutes=i + 1), 100) for i in reversed(range(200))]
    trades = [_trade(cutoff - dt.timedelta(minutes=i + 1), i) for i in reversed(range(60))]
    liq = [{
        "bucket_start": cutoff - dt.timedelta(minutes=i + 1),
        "long_liq_value": Decimal("0"), "short_liq_value": Decimal("0"),
        "long_liq_count": 0, "short_liq_count": 0,
    } for i in reversed(range(30))]
    ob = [{"source_timestamp": cutoff - dt.timedelta(minutes=i), "is_consistent": True} for i in reversed(range(10))]
    deriv = [{"source_timestamp": cutoff - dt.timedelta(minutes=5 * i), "open_interest": Decimal("1")} for i in reversed(range(10))]
    data = HistoricalInputs(c4, c1, c15, c1m, trades, liq, ob, deriv)

    ref = windows_as_of(data, cutoff)
    fast = HistoricalWindowIndex(data).windows(cutoff, include_execution_1m=True)
    assert fast.candles_4h == ref.candles_4h
    assert fast.candles_1h == ref.candles_1h
    assert fast.candles_15m == ref.candles_15m
    assert fast.trades_1m == ref.trades_1m
    assert fast.liq_15m == ref.liq_15m
    assert fast.orderbook == ref.orderbook
    assert fast.derivatives == ref.derivatives


def test_index_excludes_interval_that_only_opens_at_cutoff():
    cutoff = dt.datetime(2026, 8, 10, 12, tzinfo=UTC)
    data = HistoricalInputs(
        candles_4h=[],
        candles_1h=[_candle(cutoff - dt.timedelta(hours=1), 100), _candle(cutoff, 1)],
        candles_15m=[], candles_1m=[], trades_1m=[], liquidations_1m=[], orderbook=[], derivatives=[],
    )
    fast = HistoricalWindowIndex(data).windows(cutoff)
    assert len(fast.candles_1h) == 1
    assert fast.candles_1h[0]["open_time"] == cutoff - dt.timedelta(hours=1)
    assert fast.candles_1m == []
