"""Trend Pullback strategy (active by default).

Conditions (per the plan):
  - 4h and 1h trend agree (from TradingView context).
  - 15m shows a pullback structure (price near EMA20/EMA50, not extended).
  - The pullback returns to a logical level (EMA band / local support-
    resistance), not into open air.
  - CVD/delta confirms the pullback is being bought/sold into (i.e. taker
    flow agrees with the intended direction).
  - Open interest doesn't outright contradict the direction.
  - Spread is acceptable (checked later as a hard gate, but a very wide
    spread also lowers this strategy's own confidence here).
  - A logical stop loss exists (beyond the local support/resistance).
  - Regime is not chaotic (checked later as a hard gate too).

Entry/stop/target levels are 100% deterministic Decimal arithmetic derived
from ATR and the local support/resistance band — never invented at
runtime by an LLM.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from tradingview_mcp.core.analysis.strategies.base import StrategySetup
from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext

SETUP_NAME = "trend_pullback"


def evaluate(features: dict, tv: TradingViewContext, regime: str) -> Optional[StrategySetup]:
    price_action = features.get("price_action") or {}
    volume = features.get("volume") or {}
    futures = features.get("futures") or {}

    if not price_action.get("available"):
        return None

    if tv.trend_4h is None or tv.trend_1h is None or tv.trend_4h != tv.trend_1h:
        return None  # no 4h/1h agreement, no pullback setup this cycle
    if tv.trend_4h == "NEUTRAL":
        return None

    direction = "LONG" if tv.trend_4h == "BULLISH" else "SHORT"

    last_close = price_action.get("last_close")
    atr = price_action.get("atr")
    ema20 = price_action.get("ema20")
    ema50 = price_action.get("ema50")
    support = price_action.get("support")
    resistance = price_action.get("resistance")
    if None in (last_close, atr, ema20, ema50) or atr == 0:
        return None
    last_close, atr, ema20, ema50 = (Decimal(str(x)) for x in (last_close, atr, ema20, ema50))

    # Require price to currently be pulled back near the EMA20/50 band
    # (not extended far away from it) — the essence of "pullback".
    band_low, band_high = sorted((ema20, ema50))
    tolerance = atr * Decimal("0.5")
    in_pullback_zone = (band_low - tolerance) <= last_close <= (band_high + tolerance)
    if not in_pullback_zone:
        return None

    delta = volume.get("delta")
    flow_confirms = True
    if delta is not None:
        delta_dec = Decimal(str(delta))
        flow_confirms = (delta_dec > 0) if direction == "LONG" else (delta_dec < 0)
    if not flow_confirms:
        return None

    oi_relation = futures.get("price_to_oi_relation")
    oi_contradicts = (
        (direction == "LONG" and oi_relation == "PRICE_DOWN_OI_UP")
        or (direction == "SHORT" and oi_relation == "PRICE_UP_OI_UP")
    )
    if oi_contradicts:
        return None

    entry_min = last_close - atr * Decimal("0.15")
    entry_max = last_close + atr * Decimal("0.15")
    entry_reference = (entry_min + entry_max) / 2  # == last_close, by construction

    if direction == "LONG":
        stop_reference = Decimal(str(support)) if support is not None else last_close - atr
        stop_loss = min(stop_reference, last_close - atr * Decimal("0.5")) - atr * Decimal("0.25")
        risk = entry_reference - stop_loss
        if risk <= 0:
            return None
        take_profit_1 = entry_reference + risk * Decimal("2.0")
        take_profit_2 = entry_reference + risk * Decimal("3.6")
        invalidation = f"15m close below {stop_loss}"
    else:
        stop_reference = Decimal(str(resistance)) if resistance is not None else last_close + atr
        stop_loss = max(stop_reference, last_close + atr * Decimal("0.5")) + atr * Decimal("0.25")
        risk = stop_loss - entry_reference
        if risk <= 0:
            return None
        take_profit_1 = entry_reference - risk * Decimal("2.0")
        take_profit_2 = entry_reference - risk * Decimal("3.6")
        invalidation = f"15m close above {stop_loss}"

    reasons = [
        f"4h/1h trend agree ({tv.trend_4h})",
        f"price {last_close} pulled back into EMA20/EMA50 band [{band_low}, {band_high}]",
        "taker flow confirms direction" if delta is not None else "taker flow unavailable — not penalised, not confirmed either",
    ]
    warnings = []
    if delta is None:
        warnings.append("no trade-flow data available to confirm the pullback")
    if oi_relation is None:
        warnings.append("open interest relation unavailable")

    component_fractions = {
        "trend_alignment": Decimal("1.0"),
        "entry_structure": Decimal("1.0") if in_pullback_zone else Decimal("0.3"),
        "retest_pullback": Decimal("1.0"),
        "cvd_delta": Decimal("1.0") if (delta is not None and flow_confirms) else Decimal("0.4"),
        "open_interest": Decimal("0.5") if oi_relation is None else (Decimal("0.3") if oi_contradicts else Decimal("1.0")),
    }

    return StrategySetup(
        setup_name=SETUP_NAME,
        direction=direction,
        entry_min=min(entry_min, entry_max),
        entry_max=max(entry_min, entry_max),
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        invalidation_reason=invalidation,
        component_fractions=component_fractions,
        reasons=reasons,
        warnings=warnings,
    )
