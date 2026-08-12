from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_history_v1 import (
    HistoricalInputs,
    closed_interval_rows,
    legacy_as_is_tv_context,
    reconstruct_tv_context,
    windows_as_of,
)
from tradingview_mcp.core.backtest.comparison_v1 import TvHistoryKind


UTC = dt.timezone.utc


def _candle(ts: dt.datetime, close: str, *, volume: str = "100") -> dict:
    c = Decimal(close)
    return {
        "open_time": ts,
        "open": c,
        "high": c + Decimal("1"),
        "low": c - Decimal("1"),
        "close": c,
        "volume": Decimal(volume),
    }


def _empty_inputs(**overrides) -> HistoricalInputs:
    base = dict(
        candles_4h=[], candles_1h=[], candles_15m=[], candles_1m=[],
        trades_1m=[], liquidations_1m=[], orderbook=[], derivatives=[],
    )
    base.update(overrides)
    return HistoricalInputs(**base)


def test_in_progress_candle_is_excluded_at_cutoff():
    cutoff = dt.datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    closed = _candle(dt.datetime(2026, 8, 10, 11, 0, tzinfo=UTC), "100")
    opens_at_cutoff = _candle(cutoff, "999")
    rows = closed_interval_rows(
        [closed, opens_at_cutoff],
        timestamp_key="open_time",
        duration=dt.timedelta(hours=1),
        cutoff=cutoff,
    )
    assert rows == [closed]


def test_one_minute_execution_candle_opening_at_cutoff_is_not_decision_input():
    cutoff = dt.datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    previous = _candle(dt.datetime(2026, 8, 10, 11, 59, tzinfo=UTC), "100")
    future = _candle(cutoff, "500")
    w = windows_as_of(_empty_inputs(candles_1m=[previous, future]), cutoff)
    # 11:59 candle closed exactly at 12:00 and is knowable. The 12:00 candle
    # only starts at the decision timestamp and belongs to execution future.
    assert len(w.candles_1m) == 1
    assert w.candles_1m[0]["open_time"] == previous["open_time"]


def test_reconstructed_1h_bias_uses_only_closed_history():
    cutoff = dt.datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    c1 = []
    # 25 steadily rising CLOSED bars ending with the 11:00-12:00 candle.
    for i in range(25):
        ts = cutoff - dt.timedelta(hours=25 - i)
        c1.append(_candle(ts, str(100 + i)))
    # A huge bearish-looking future bar starts at cutoff. It must not leak in.
    c1.append(_candle(cutoff, "1"))
    w = windows_as_of(_empty_inputs(candles_1h=c1), cutoff)
    ctx = reconstruct_tv_context("BTCUSDT", w)
    assert ctx.trend_1h == "BULLISH"
    assert ctx.raw["history_kind"] == TvHistoryKind.RECONSTRUCTED.value


def test_reconstructed_4h_requires_enough_ema50_history():
    cutoff = dt.datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    c4 = []
    for i in range(30):
        ts = cutoff - dt.timedelta(hours=4 * (30 - i))
        c4.append(_candle(ts, str(100 + i)))
    w = windows_as_of(_empty_inputs(candles_4h=c4), cutoff)
    ctx = reconstruct_tv_context("BTCUSDT", w)
    assert ctx.trend_4h is None


def test_legacy_as_is_explicitly_preserves_parser_mismatch():
    ctx = legacy_as_is_tv_context("BTCUSDT")
    assert ctx.available is True
    assert ctx.trend_15m is None
    assert ctx.trend_1h is None
    assert ctx.trend_4h is None
    assert ctx.raw["legacy_as_is_parser_mismatch_preserved"] is True
