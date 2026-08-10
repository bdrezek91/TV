"""TV External Confirmation v1 — historical SHADOW implementation.

This is *not* a true point-in-time TradingView feed. It reconstructs a compact
classic-TA view from closed Bybit candles and is therefore labelled
``TV_RECONSTRUCTED_CLASSIC_TA`` everywhere. The result can stratify historical
Research V2 outcomes, but it never creates/changes a setup, direction, entry,
SL, TP, size, W4 decision, or W5 execution.

The four reporting dimensions mirror the intended live external-confirmation
contract while explicitly tagging feature overlap with W1/W2 so the backtest
does not pretend they are independent evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.backtest.comparison_history_v1 import PointInTimeWindows, reconstruct_tv_context
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal
from tradingview_mcp.core.services.indicators_calc import calc_atr, calc_bollinger, calc_macd, calc_rsi


TV_SHADOW_VERSION = "TV_RECONSTRUCTED_CLASSIC_TA_V1"
STATUSES = ("CONFIRMED", "WEAK_CONFIRMATION", "NEUTRAL", "CONTRADICTED", "INSUFFICIENT_DATA")


@dataclass(frozen=True)
class TvShadowResult:
    status: str
    score: Decimal
    confidence: Decimal
    dimensions: Mapping[str, Any]
    reasons: tuple[str, ...]
    counterarguments: tuple[str, ...]
    history_kind: str = TV_SHADOW_VERSION
    can_affect_decision: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "confidence": self.confidence,
            "dimensions": dict(self.dimensions),
            "reasons": list(self.reasons),
            "counterarguments": list(self.counterarguments),
            "history_kind": self.history_kind,
            "can_affect_decision": self.can_affect_decision,
        }


def _last(values):
    return values[-1] if values else None


def _dimension(label: str, score: int, detail: str, overlap: str) -> dict[str, Any]:
    return {"status": label, "score": score, "detail": detail, "overlap_class": overlap}


def _direction_label(signal: NeutralSignal) -> str:
    return "BULLISH" if signal.direction == "LONG" else "BEARISH"


def _htf_dimension(signal: NeutralSignal, windows: PointInTimeWindows) -> dict[str, Any]:
    tv = reconstruct_tv_context(signal.symbol, windows)
    desired = _direction_label(signal)
    trends = [x for x in (tv.trend_1h, tv.trend_4h) if x is not None]
    if len(trends) < 2:
        return _dimension("INSUFFICIENT_DATA", 0, "1h/4h bias unavailable", "ALREADY_USED")
    opposite = "BEARISH" if desired == "BULLISH" else "BULLISH"
    if all(x == desired for x in trends):
        return _dimension("ALIGNED", 1, f"1h+4h {desired}", "ALREADY_USED")
    if all(x == opposite for x in trends):
        return _dimension("CONTRADICTED", -1, f"1h+4h {opposite}", "ALREADY_USED")
    if opposite in trends and desired not in trends:
        return _dimension("CONTRADICTED", -1, f"HTF leans opposite: {trends}", "ALREADY_USED")
    return _dimension("MIXED", 0, f"HTF mixed: {trends}", "ALREADY_USED")


def _momentum_dimension(signal: NeutralSignal, windows: PointInTimeWindows) -> dict[str, Any]:
    closes = [float(c["close"]) for c in windows.candles_1h]
    if len(closes) < 35:
        return _dimension("INSUFFICIENT_DATA", 0, "<35 closed 1h bars", "PARTIAL_OVERLAP")
    rsi = _last(calc_rsi(closes, 14))
    macd = calc_macd(closes, 12, 26, 9)
    macd_line, macd_signal = _last(macd["macd"]), _last(macd["signal"])
    if rsi is None or macd_line is None or macd_signal is None:
        return _dimension("INSUFFICIENT_DATA", 0, "RSI/MACD warm-up incomplete", "PARTIAL_OVERLAP")

    if signal.direction == "LONG":
        aligned = rsi >= 52 and macd_line > macd_signal
        opposite = rsi <= 48 and macd_line < macd_signal
    else:
        aligned = rsi <= 48 and macd_line < macd_signal
        opposite = rsi >= 52 and macd_line > macd_signal
    detail = f"RSI14={rsi:.2f}, MACD={'bullish' if macd_line > macd_signal else 'bearish'}"
    if aligned:
        return _dimension("ALIGNED", 1, detail, "PARTIAL_OVERLAP")
    if opposite:
        return _dimension("CONTRADICTED", -1, detail, "PARTIAL_OVERLAP")
    return _dimension("MIXED", 0, detail, "PARTIAL_OVERLAP")


def _location_dimension(signal: NeutralSignal, windows: PointInTimeWindows) -> dict[str, Any]:
    closes = [float(c["close"]) for c in windows.candles_1h]
    if len(closes) < 20:
        return _dimension("INSUFFICIENT_DATA", 0, "<20 closed 1h bars", "PARTIAL_OVERLAP")
    bb = calc_bollinger(closes, 20, 2.0)
    price = closes[-1]
    upper, middle, lower = _last(bb["upper"]), _last(bb["middle"]), _last(bb["lower"])
    if None in (upper, middle, lower):
        return _dimension("INSUFFICIENT_DATA", 0, "Bollinger warm-up incomplete", "PARTIAL_OVERLAP")

    if signal.direction == "LONG":
        if price > upper:
            return _dimension("EXTENDED", 0, "price above upper Bollinger band", "PARTIAL_OVERLAP")
        if price >= middle:
            return _dimension("ALIGNED", 1, "price in bullish half of Bollinger envelope", "PARTIAL_OVERLAP")
        if price < lower:
            return _dimension("CONTRADICTED", -1, "price below lower Bollinger band", "PARTIAL_OVERLAP")
    else:
        if price < lower:
            return _dimension("EXTENDED", 0, "price below lower Bollinger band", "PARTIAL_OVERLAP")
        if price <= middle:
            return _dimension("ALIGNED", 1, "price in bearish half of Bollinger envelope", "PARTIAL_OVERLAP")
        if price > upper:
            return _dimension("CONTRADICTED", -1, "price above upper Bollinger band", "PARTIAL_OVERLAP")
    return _dimension("MIXED", 0, "price on opposite half but inside Bollinger envelope", "PARTIAL_OVERLAP")


def _volatility_dimension(signal: NeutralSignal, windows: PointInTimeWindows) -> dict[str, Any]:
    highs = [float(c["high"]) for c in windows.candles_1h]
    lows = [float(c["low"]) for c in windows.candles_1h]
    closes = [float(c["close"]) for c in windows.candles_1h]
    if len(closes) < 15:
        return _dimension("INSUFFICIENT_DATA", 0, "<15 closed 1h bars", "PARTIAL_OVERLAP")
    atr = _last(calc_atr(highs, lows, closes, 14))
    if atr is None or closes[-1] <= 0:
        return _dimension("INSUFFICIENT_DATA", 0, "ATR unavailable", "PARTIAL_OVERLAP")
    atr_pct = atr / closes[-1] * 100

    # Broad hypothesis bands: this dimension should flag only obvious mismatch,
    # not micro-optimise a strategy on history.
    if signal.setup_name == "mean_reversion":
        ok = 0.10 <= atr_pct <= 3.0
    elif signal.setup_name in {"breakout", "breakout_retest"}:
        ok = 0.20 <= atr_pct <= 5.0
    elif "liquidation" in signal.setup_name:
        ok = atr_pct >= 0.50
    else:  # trend pullback / unknown classic setup
        ok = 0.15 <= atr_pct <= 4.0
    return _dimension(
        "ALIGNED" if ok else "UNSUITABLE",
        1 if ok else -1,
        f"ATR14={atr_pct:.3f}%",
        "PARTIAL_OVERLAP",
    )


def evaluate_tv_shadow(signal: NeutralSignal, windows: PointInTimeWindows) -> TvShadowResult:
    """Evaluate TV-style context known at the signal cutoff; never mutate it."""
    signal.validate()
    dims = {
        "htf_structure": _htf_dimension(signal, windows),
        "momentum": _momentum_dimension(signal, windows),
        "location": _location_dimension(signal, windows),
        "volatility": _volatility_dimension(signal, windows),
    }
    available = [d for d in dims.values() if d["status"] != "INSUFFICIENT_DATA"]
    if len(available) < 3:
        return TvShadowResult(
            "INSUFFICIENT_DATA", Decimal("0"), Decimal(str(len(available) / 4)), dims,
            (), ("fewer than 3/4 TV-style dimensions available",),
        )

    # Equal dimension groups avoid counting five correlated indicators as five
    # independent votes. HTF is still explicitly tagged ALREADY_USED above.
    raw = sum(int(d["score"]) for d in available) / len(available)
    score = Decimal(str(round(raw, 4)))
    confidence = Decimal(str(round(len(available) / 4, 4)))
    if raw >= 0.50:
        status = "CONFIRMED"
    elif raw >= 0.20:
        status = "WEAK_CONFIRMATION"
    elif raw <= -0.50:
        status = "CONTRADICTED"
    else:
        status = "NEUTRAL"

    reasons = tuple(f"{name}: {d['detail']}" for name, d in dims.items() if d["score"] > 0)
    counter = tuple(f"{name}: {d['detail']}" for name, d in dims.items() if d["score"] < 0)
    return TvShadowResult(status, score, confidence, dims, reasons, counter)
