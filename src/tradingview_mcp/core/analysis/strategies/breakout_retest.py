"""Breakout + Retest strategy (active by default).

Conditions (per the plan):
  - Prior compression/consolidation (structure == "CONTRACTING" from the
    Feature Engine's swing-structure detector, or price sitting in the
    tight middle of its recent range).
  - Volume increase at the breakout (volume z-score above a threshold).
  - No entry directly after an overextended breakout candle (distance from
    EMA20 must not already be extreme).
  - A retest of the broken level that holds (price pulled back near
    support/resistance without breaking back through it).
  - CVD/delta confirms.
  - OI supports or doesn't contradict the move.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from tradingview_mcp.core.analysis.strategies.base import StrategySetup
from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext

SETUP_NAME = "breakout_retest"
VOLUME_ZSCORE_THRESHOLD = Decimal("1.0")
MAX_EXTENSION_PCT = Decimal("2.5")  # % distance from EMA20 beyond which we call it "overextended"


def evaluate(features: dict, tv: TradingViewContext, regime: str) -> Optional[StrategySetup]:
    price_action = features.get("price_action") or {}
    volume = features.get("volume") or {}
    futures = features.get("futures") or {}

    if not price_action.get("available"):
        return None

    structure = price_action.get("structure")
    if structure not in ("CONTRACTING", "EXPANDING"):
        return None  # no compression-then-expansion pattern detected

    position_in_range = price_action.get("position_in_range")
    support = price_action.get("support")
    resistance = price_action.get("resistance")
    last_close = price_action.get("last_close")
    atr = price_action.get("atr")
    ema20 = price_action.get("ema20")
    if None in (position_in_range, support, resistance, last_close, atr, ema20) or atr == 0:
        return None

    position_in_range = Decimal(str(position_in_range))
    support, resistance, last_close, atr, ema20 = (
        Decimal(str(x)) for x in (support, resistance, last_close, atr, ema20)
    )

    volume_z = volume.get("volume_zscore")
    volume_confirms = volume_z is not None and Decimal(str(volume_z)) >= VOLUME_ZSCORE_THRESHOLD

    extension_pct = abs((last_close - ema20) / ema20 * Decimal("100")) if ema20 else Decimal("0")
    overextended = extension_pct > MAX_EXTENSION_PCT

    if position_in_range >= Decimal("0.85"):
        direction = "LONG"
        broken_level = resistance
    elif position_in_range <= Decimal("0.15"):
        direction = "SHORT"
        broken_level = support
    else:
        return None  # not currently near either edge of the range — no breakout to retest

    if overextended:
        return None  # don't chase; wait for the retest to actually happen

    # Retest: price must be back near the broken level (within 0.5 ATR),
    # confirming the level "held" rather than broke straight through it.
    if abs(last_close - broken_level) > atr * Decimal("0.5"):
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

    entry_min = last_close - atr * Decimal("0.1")
    entry_max = last_close + atr * Decimal("0.1")
    entry_reference = (entry_min + entry_max) / 2  # == last_close, by construction

    if direction == "LONG":
        stop_loss = broken_level - atr * Decimal("0.5")
        risk = entry_reference - stop_loss
        if risk <= 0:
            return None
        take_profit_1 = entry_reference + risk * Decimal("2.0")
        take_profit_2 = entry_reference + risk * Decimal("3.0")
        invalidation = f"15m close back below {stop_loss}"
    else:
        stop_loss = broken_level + atr * Decimal("0.5")
        risk = stop_loss - entry_reference
        if risk <= 0:
            return None
        take_profit_1 = entry_reference - risk * Decimal("2.0")
        take_profit_2 = entry_reference - risk * Decimal("3.0")
        invalidation = f"15m close back above {stop_loss}"

    reasons = [
        f"prior compression/expansion ({structure}) with a retest of the broken level {broken_level}",
        "volume confirms the breakout" if volume_confirms else "volume did not clearly confirm the breakout",
    ]
    warnings = []
    if not volume_confirms:
        warnings.append("breakout volume z-score below confirmation threshold")
    if oi_relation is None:
        warnings.append("open interest relation unavailable")

    component_fractions = {
        "entry_structure": Decimal("1.0"),
        "retest_pullback": Decimal("1.0"),
        "volume": Decimal("1.0") if volume_confirms else Decimal("0.4"),
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
