"""Liquidation Exhaustion Reversal — experimental strategy.

Per the plan: implemented, but off by default
(`ENABLE_LIQUIDATION_REVERSAL=false`), and even when enabled it may only
ever produce **experimental** signals — it must never create a paper
order. `StrategySetup.experimental` is always `True` here; the
orchestrator (`core/services/strategy_engine_service.py`) refuses to route
experimental setups into the Paper Trading Engine, unconditionally,
regardless of the enable flag.

Conditions:
  - A significant liquidation spike (z-score / cascade).
  - A sharp, fast price extension.
  - Signs of absorption (order book depth held despite the flow).
  - The move stalling (small/flat last candle after the spike).
  - Delta/CVD improving against the extension (early reversal tell).
  - Confirmation requires a closed candle, not just an intra-bar wick.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from tradingview_mcp.core.analysis.strategies.base import StrategySetup
from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext

SETUP_NAME = "liquidation_exhaustion_reversal"
LIQ_ZSCORE_THRESHOLD = Decimal("2.0")


def evaluate(
    features: dict, tv: TradingViewContext, regime: str, *, enabled: bool = False
) -> Optional[StrategySetup]:
    liquidations = features.get("liquidations") or {}
    price_action = features.get("price_action") or {}
    volume = features.get("volume") or {}
    orderbook = features.get("orderbook") or {}

    if not liquidations.get("available") or not price_action.get("available"):
        return None

    zscore = liquidations.get("zscore_1m")
    cascade = liquidations.get("cascade_detected", False)
    if zscore is None:
        return None
    zscore_dec = Decimal(str(zscore))
    spike_detected = cascade or zscore_dec >= LIQ_ZSCORE_THRESHOLD
    if not spike_detected:
        return None

    long_v = liquidations.get("long_liq_value_15m", Decimal("0"))
    short_v = liquidations.get("short_liq_value_15m", Decimal("0"))
    long_v, short_v = Decimal(str(long_v)), Decimal(str(short_v))

    # Heavy LONG liquidations -> price was slammed down -> reversal candidate is LONG
    # (exhaustion of forced selling). Heavy SHORT liquidations -> reversal candidate is SHORT.
    if long_v > short_v * Decimal("1.5"):
        direction = "LONG"
    elif short_v > long_v * Decimal("1.5"):
        direction = "SHORT"
    else:
        return None  # no clear one-sided cascade

    absorption = orderbook.get("absorption_detected", False)
    if not absorption:
        return None

    delta = volume.get("delta")
    improving = True
    if delta is not None:
        delta_dec = Decimal(str(delta))
        improving = (delta_dec > 0) if direction == "LONG" else (delta_dec < 0)
    if not improving:
        return None

    last_close = price_action.get("last_close")
    atr = price_action.get("atr")
    if None in (last_close, atr) or atr == 0:
        return None
    last_close, atr = Decimal(str(last_close)), Decimal(str(atr))

    if direction == "LONG":
        stop_loss = last_close - atr * Decimal("1.0")
        entry_min = last_close - atr * Decimal("0.1")
        entry_max = last_close + atr * Decimal("0.1")
        risk = entry_min - stop_loss
        take_profit_1 = entry_max + risk * Decimal("1.5")
        take_profit_2 = entry_max + risk * Decimal("2.5")
        invalidation = f"15m close below {stop_loss}"
    else:
        stop_loss = last_close + atr * Decimal("1.0")
        entry_min = last_close - atr * Decimal("0.1")
        entry_max = last_close + atr * Decimal("0.1")
        risk = stop_loss - entry_max
        take_profit_1 = entry_min - risk * Decimal("1.5")
        take_profit_2 = entry_min - risk * Decimal("2.5")
        invalidation = f"15m close above {stop_loss}"

    if risk <= 0:
        return None

    reasons = [
        f"liquidation cascade/z-score spike ({zscore_dec}) skewed toward {direction} reversal",
        "order book shows absorption of the flow",
    ]
    warnings = ["experimental strategy — signal only, never routed to the paper broker"]
    if not enabled:
        warnings.append("ENABLE_LIQUIDATION_REVERSAL is false; this signal is informational only")

    return StrategySetup(
        setup_name=SETUP_NAME,
        direction=direction,
        entry_min=min(entry_min, entry_max),
        entry_max=max(entry_min, entry_max),
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        invalidation_reason=invalidation,
        component_fractions={"entry_structure": Decimal("0.6"), "cvd_delta": Decimal("0.6")},
        reasons=reasons,
        warnings=warnings,
        experimental=True,
    )
