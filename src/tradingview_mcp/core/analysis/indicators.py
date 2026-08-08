"""Deterministic, Decimal-safe price-action indicators computed directly
from Bybit candle history (independent of / complementary to whatever
TradingView MCP's own indicator tools report for higher-timeframe context).

All functions are pure and take/return `Decimal` — no floats leak into the
public API, per the plan's absolute rule for price-derived values.
"""
from __future__ import annotations

from decimal import Decimal, getcontext
from typing import List, Optional, Sequence

getcontext().prec = 40


def ema(values: Sequence[Decimal], period: int) -> Optional[Decimal]:
    """Standard exponential moving average; returns the last EMA value.

    Uses a simple-average seed over the first `period` values (the common,
    deterministic convention), then applies the EMA recurrence over the
    rest. Returns None if there isn't enough history.
    """
    if len(values) < period:
        return None
    k = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    result = seed
    for v in values[period:]:
        result = v * k + result * (Decimal(1) - k)
    return result


def ema_series(values: Sequence[Decimal], period: int) -> List[Decimal]:
    """Full EMA series (same length convention as `ema`, but returns every
    value from the seed point onward) — used internally for slope calcs."""
    if len(values) < period:
        return []
    k = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (Decimal(1) - k))
    return out


def atr(candles: Sequence[dict], period: int) -> Optional[Decimal]:
    """Average True Range over `period` bars using Wilder's method (simple
    average seed, then smoothed). Each candle dict needs high/low/close."""
    if len(candles) < period + 1:
        return None
    true_ranges: List[Decimal] = []
    for i in range(1, len(candles)):
        high = Decimal(str(candles[i]["high"]))
        low = Decimal(str(candles[i]["low"]))
        prev_close = Decimal(str(candles[i - 1]["close"]))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return None
    seed = sum(true_ranges[:period], Decimal("0")) / Decimal(period)
    result = seed
    for tr in true_ranges[period:]:
        result = (result * Decimal(period - 1) + tr) / Decimal(period)
    return result


def atr_pct(candles: Sequence[dict], period: int) -> Optional[Decimal]:
    value = atr(candles, period)
    if value is None or not candles:
        return None
    last_close = Decimal(str(candles[-1]["close"]))
    if last_close == 0:
        return None
    return (value / last_close) * Decimal("100")


def realized_volatility(closes: Sequence[Decimal], period: int) -> Optional[Decimal]:
    """Stdev of simple returns over the trailing `period` closes, expressed
    as a percentage. Deterministic population-stdev (no Bessel correction)
    to keep the exact figure reproducible bar-for-bar."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    returns = [
        (window[i] - window[i - 1]) / window[i - 1]
        for i in range(1, len(window))
        if window[i - 1] != 0
    ]
    if not returns:
        return None
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / Decimal(len(returns))
    std = variance.sqrt()  # Decimal has a native sqrt() via the active context
    return std * Decimal("100")


def vwap(candles: Sequence[dict]) -> Optional[Decimal]:
    """Volume-weighted average price over the given candle window (typical
    price × volume, summed and divided by total volume)."""
    if not candles:
        return None
    total_pv = Decimal("0")
    total_v = Decimal("0")
    for c in candles:
        high = Decimal(str(c["high"]))
        low = Decimal(str(c["low"]))
        close = Decimal(str(c["close"]))
        volume = Decimal(str(c["volume"]))
        typical = (high + low + close) / Decimal(3)
        total_pv += typical * volume
        total_v += volume
    if total_v == 0:
        return None
    return total_pv / total_v


def position_in_range(candles: Sequence[dict]) -> Optional[Decimal]:
    """Where the last close sits within the window's high/low range, as a
    0..1 fraction (0 = at the low, 1 = at the high)."""
    if not candles:
        return None
    highs = [Decimal(str(c["high"])) for c in candles]
    lows = [Decimal(str(c["low"])) for c in candles]
    day_high, day_low = max(highs), min(lows)
    if day_high == day_low:
        return Decimal("0.5")
    last_close = Decimal(str(candles[-1]["close"]))
    return (last_close - day_low) / (day_high - day_low)


def local_support_resistance(candles: Sequence[dict], lookback: int = 20) -> dict:
    """Naive local S/R: min low / max high over the trailing `lookback` bars."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    if not window:
        return {"support": None, "resistance": None}
    return {
        "support": min(Decimal(str(c["low"])) for c in window),
        "resistance": max(Decimal(str(c["high"])) for c in window),
    }


def swing_structure(candles: Sequence[dict], swing_window: int = 3) -> str:
    """Classifies recent price structure as HH (higher highs), HL (higher
    lows i.e. an uptrend pullback structure), LH (lower highs), LL (lower
    lows), or UNKNOWN with insufficient data.

    Uses a simple fractal-swing detector: a bar is a swing high if its high
    is the max within +/-`swing_window` bars, a swing low symmetrically.
    Compares the two most recent swing highs and the two most recent swing
    lows to classify.
    """
    n = len(candles)
    if n < swing_window * 2 + 3:
        return "UNKNOWN"

    highs = [Decimal(str(c["high"])) for c in candles]
    lows = [Decimal(str(c["low"])) for c in candles]

    swing_highs = []
    swing_lows = []
    for i in range(swing_window, n - swing_window):
        window_highs = highs[i - swing_window: i + swing_window + 1]
        window_lows = lows[i - swing_window: i + swing_window + 1]
        if highs[i] == max(window_highs):
            swing_highs.append(highs[i])
        if lows[i] == min(window_lows):
            swing_lows.append(lows[i])

    higher_high = len(swing_highs) >= 2 and swing_highs[-1] > swing_highs[-2]
    higher_low = len(swing_lows) >= 2 and swing_lows[-1] > swing_lows[-2]
    lower_high = len(swing_highs) >= 2 and swing_highs[-1] < swing_highs[-2]
    lower_low = len(swing_lows) >= 2 and swing_lows[-1] < swing_lows[-2]

    if higher_high and higher_low:
        return "HH_HL"  # healthy uptrend structure
    if lower_high and lower_low:
        return "LH_LL"  # healthy downtrend structure
    if higher_high and lower_low:
        return "EXPANDING"
    if lower_high and higher_low:
        return "CONTRACTING"
    return "UNKNOWN"
