"""Price Setup Detector -- Warstwa 2 (research layer).

Detects candidate LONG/SHORT setups from already-classified market regime
(Warstwa 1, `regime_classifier_v2`) plus the same price-action/volume/
orderbook/futures/liquidation feature dicts `feature_engine` produces.
Pure functions only: no DB/network access, no order execution of any
kind -- this module NEVER places, modifies, or cancels a real order. It
only describes a candidate trade idea for a human or a later risk-
management layer (Warstwa 4) to act on.

Four setup types, each gated by the regime it depends on -- a setup type
is NEVER evaluated positively against an incompatible regime:

  trend_pullback      -- requires TREND_UP / TREND_DOWN
  breakout            -- requires BREAKOUT_UP / BREAKOUT_DOWN (SQUEEZE is
                          "watched" but produces NO_SETUP: direction isn't
                          resolved yet)
  mean_reversion       -- requires RANGE
  liquidation_reversal -- requires PANIC_LIQUIDATION_LONGS / _SHORTS

`detect_setups()` evaluates all four for a symbol/timestamp and returns
one dict per type -- most calls will see NO_SETUP for 3 of the 4 simply
because the regime doesn't support them, which is correct, not a bug.

Every setup result carries: direction (LONG/SHORT/NO_SETUP), entry_zone,
invalidation, stop_loss, targets, confidence, reasons, counterarguments,
regime_compatible, orderflow_confirmed (False -> price_only_mode, setup
still fires but flagged as unconfirmed by order flow, with reduced
confidence).

All thresholds marked "HYPOTHESIS" are reasonable starting points, not
tuned against this project's own data yet -- validate via a future
setup-specific backtest before trusting them beyond directional research.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

SETUP_TYPES = ("trend_pullback", "breakout", "mean_reversion", "liquidation_reversal")

# Which primary regimes each setup type is compatible with. A setup is
# NEVER produced (beyond NO_SETUP) against a regime outside this set.
REGIME_COMPATIBILITY = {
    "trend_pullback": {"TREND_UP", "TREND_DOWN"},
    "breakout": {"BREAKOUT_UP", "BREAKOUT_DOWN", "SQUEEZE"},
    "mean_reversion": {"RANGE"},
    "liquidation_reversal": {"PANIC_LIQUIDATION_LONGS", "PANIC_LIQUIDATION_SHORTS"},
}

# --- HYPOTHESIS thresholds: reasonable starting points, not yet tuned ---
SETUP_CONFIDENCE_FLOOR = Decimal("0.30")
PULLBACK_EXTENDED_ATR_MULT = Decimal("3.0")   # beyond this from ema20, trend is "extended", not pulling back
MEAN_REVERSION_EXTREME = Decimal("0.20")      # position_in_range <= this (LONG) or >= 1-this (SHORT)
BREAKOUT_ENTRY_ATR_MULT = Decimal("0.3")
LIQ_REVERSAL_ENTRY_ATR_MULT = Decimal("0.5")
SL_ATR_BUFFER_MULT = Decimal("0.5")
VOLUME_ZSCORE_CONFIRM = Decimal("1.0")


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _no_setup(setup_type: str, regime_compatible: bool, reasons: List[str]) -> dict:
    return {
        "setup_type": setup_type,
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": reasons,
        "counterarguments": [],
        "regime_compatible": regime_compatible,
        "orderflow_confirmed": False,
        "price_only_mode": False,
    }


def _calibrate(base: float, confirmations: int, contradictions: int, regime_confidence: float) -> float:
    """A setup can never be more confident than the regime call it's built
    on -- garbage regime in, garbage setup out."""
    score = base + 0.05 * confirmations - 0.10 * contradictions
    score = min(score, regime_confidence + 0.05)
    return round(max(0.05, min(0.90, score)), 2)


def _finalize(decision: dict, regime_confidence: float) -> dict:
    confirmations = decision.pop("_confirmations", 0)
    contradictions = decision.pop("_contradictions", len(decision.get("counterarguments", [])))
    confidence = _calibrate(decision.pop("_base_confidence"), confirmations, contradictions, regime_confidence)
    if confidence < float(SETUP_CONFIDENCE_FLOOR):
        return _no_setup(
            decision["setup_type"], True,
            [f"calibrated confidence {confidence} below the actionable floor {SETUP_CONFIDENCE_FLOOR}",
             f"best candidate before the floor check was {decision['direction']}"] + decision.get("reasons", []),
        )
    decision["confidence"] = confidence
    return decision


# ── trend_pullback ──────────────────────────────────────────────────────

def _trend_pullback(symbol: str, regime: dict, tf: Dict[str, dict], volume: dict) -> dict:
    setup_type = "trend_pullback"
    primary = regime["primary_regime"]
    if primary not in REGIME_COMPATIBILITY[setup_type]:
        return _no_setup(setup_type, False, [f"regime {primary} does not support a trend-pullback setup"])

    pa_1h = tf.get("1h") or {}
    if not pa_1h.get("available"):
        return _no_setup(setup_type, True, ["1h price-action unavailable -- cannot locate the pullback zone"])

    direction = "LONG" if primary == "TREND_UP" else "SHORT"
    last_close = _d(pa_1h.get("last_close"))
    ema20 = _d(pa_1h.get("ema20"))
    ema50 = _d(pa_1h.get("ema50"))
    atr = _d(pa_1h.get("atr"))
    support = _d(pa_1h.get("support"))
    resistance = _d(pa_1h.get("resistance"))
    if None in (last_close, ema20, ema50, atr):
        return _no_setup(setup_type, True, ["insufficient 1h EMA/ATR history to define a pullback zone"])

    # "Extended" trend (price too far from ema20) has no pullback in
    # progress right now -- that's a real, expected NO_SETUP, not a bug.
    distance_atr = abs(last_close - ema20) / atr if atr > 0 else Decimal("0")
    if distance_atr > PULLBACK_EXTENDED_ATR_MULT:
        return _no_setup(setup_type, True,
                          [f"trend intact but price is {distance_atr:.1f}x ATR from ema20 -- extended, no pullback in progress"])

    zone_lo, zone_hi = min(ema20, ema50), max(ema20, ema50)
    in_zone = zone_lo <= last_close <= zone_hi or distance_atr <= Decimal("1.0")
    if not in_zone:
        return _no_setup(setup_type, True, ["trend intact but price has not pulled back into the ema20/ema50 zone yet"])

    reasons = [f"{primary} regime with {regime['confidence']} confidence",
               "price has pulled back into the ema20/ema50 zone within an established trend"]
    counter = []
    invalidation = (support if support is not None else zone_lo * Decimal("0.99")) if direction == "LONG" else \
                   (resistance if resistance is not None else zone_hi * Decimal("1.01"))
    stop_loss = invalidation - atr * SL_ATR_BUFFER_MULT if direction == "LONG" else invalidation + atr * SL_ATR_BUFFER_MULT
    risk = abs(last_close - stop_loss)
    targets = [last_close + risk * Decimal(m) if direction == "LONG" else last_close - risk * Decimal(m)
               for m in ("1.5", "2.5", "4.0")]
    if resistance is not None and direction == "LONG":
        targets[0] = max(targets[0], resistance)
    if support is not None and direction == "SHORT":
        targets[0] = min(targets[0], support)

    confirmations, contradictions = 0, 0
    orderflow_confirmed = False
    if volume.get("available"):
        cvd_slope = _d(volume.get("cvd_slope_15m"))
        if cvd_slope is not None:
            aligned = (direction == "LONG" and cvd_slope > 0) or (direction == "SHORT" and cvd_slope < 0)
            if aligned:
                reasons.append("CVD slope confirms continuation direction into the pullback")
                confirmations += 1
                orderflow_confirmed = True
            else:
                counter.append("CVD slope has not yet turned back in the trend direction")
                contradictions += 1

    return {
        "setup_type": setup_type, "direction": direction,
        "entry_zone": {"low": zone_lo, "high": zone_hi},
        "invalidation": invalidation, "stop_loss": stop_loss, "targets": targets,
        "reasons": reasons, "counterarguments": counter,
        "regime_compatible": True, "orderflow_confirmed": orderflow_confirmed,
        "price_only_mode": not volume.get("available", False),
        "_base_confidence": 0.55, "_confirmations": confirmations, "_contradictions": contradictions,
    }


# ── breakout ─────────────────────────────────────────────────────────────

def _breakout(symbol: str, regime: dict, tf: Dict[str, dict], volume: dict, futures: dict) -> dict:
    setup_type = "breakout"
    primary = regime["primary_regime"]
    if primary not in REGIME_COMPATIBILITY[setup_type]:
        return _no_setup(setup_type, False, [f"regime {primary} does not support a breakout setup"])
    if primary == "SQUEEZE":
        return _no_setup(setup_type, True, ["squeeze detected (pre-breakout, watched) but direction is not confirmed yet"])

    pa_1h = tf.get("1h") or {}
    if not pa_1h.get("available"):
        return _no_setup(setup_type, True, ["1h price-action unavailable -- cannot locate the breakout level"])

    direction = "LONG" if primary == "BREAKOUT_UP" else "SHORT"
    last_close = _d(pa_1h.get("last_close"))
    atr = _d(pa_1h.get("atr"))
    support = _d(pa_1h.get("support"))
    resistance = _d(pa_1h.get("resistance"))
    if None in (last_close, atr, support, resistance) or support >= resistance:
        return _no_setup(setup_type, True, ["insufficient 1h range/ATR history to define the breakout level"])

    level = resistance if direction == "LONG" else support
    range_size = resistance - support
    reasons = [f"{primary} regime with {regime['confidence']} confidence",
               f"breakout level at {level}, prior range size {range_size}"]
    counter = []
    entry_zone = {"low": last_close - atr * BREAKOUT_ENTRY_ATR_MULT, "high": last_close + atr * BREAKOUT_ENTRY_ATR_MULT}
    invalidation = support - atr * Decimal("0.1") if direction == "LONG" else resistance + atr * Decimal("0.1")
    stop_loss = invalidation - atr * SL_ATR_BUFFER_MULT if direction == "LONG" else invalidation + atr * SL_ATR_BUFFER_MULT
    targets = [level + range_size * Decimal(m) if direction == "LONG" else level - range_size * Decimal(m)
               for m in ("0.5", "1.0", "1.5")]

    confirmations, contradictions = 0, 0
    orderflow_confirmed = False
    if volume.get("available"):
        volume_z = _d(volume.get("volume_zscore"))
        if volume_z is not None and volume_z >= VOLUME_ZSCORE_CONFIRM:
            reasons.append(f"volume z-score {volume_z} confirms breakout participation")
            confirmations += 1
            orderflow_confirmed = True
        elif volume_z is not None:
            counter.append("volume has not expanded to confirm the breakout")
            contradictions += 1
    if futures.get("available"):
        oi = _d(futures.get("oi_change_1h_pct"))
        if oi is not None and oi > 0:
            reasons.append(f"open interest rising (+{oi}% over 1h) alongside the breakout")
            confirmations += 1

    return {
        "setup_type": setup_type, "direction": direction,
        "entry_zone": entry_zone, "invalidation": invalidation, "stop_loss": stop_loss, "targets": targets,
        "reasons": reasons, "counterarguments": counter,
        "regime_compatible": True, "orderflow_confirmed": orderflow_confirmed,
        "price_only_mode": not volume.get("available", False),
        "_base_confidence": 0.55, "_confirmations": confirmations, "_contradictions": contradictions,
    }


# ── mean_reversion ───────────────────────────────────────────────────────

def _mean_reversion(symbol: str, regime: dict, tf: Dict[str, dict], volume: dict) -> dict:
    setup_type = "mean_reversion"
    primary = regime["primary_regime"]
    if primary not in REGIME_COMPATIBILITY[setup_type]:
        return _no_setup(setup_type, False, [f"regime {primary} does not support a mean-reversion setup"])

    pa_1h = tf.get("1h") or {}
    if not pa_1h.get("available"):
        return _no_setup(setup_type, True, ["1h price-action unavailable -- cannot locate range extremes"])

    last_close = _d(pa_1h.get("last_close"))
    atr = _d(pa_1h.get("atr"))
    support = _d(pa_1h.get("support"))
    resistance = _d(pa_1h.get("resistance"))
    pos = _d(pa_1h.get("position_in_range"))
    if None in (last_close, atr, support, resistance, pos) or support >= resistance:
        return _no_setup(setup_type, True, ["insufficient 1h range/ATR history to define range extremes"])

    if pos <= MEAN_REVERSION_EXTREME:
        direction = "LONG"
    elif pos >= (Decimal("1") - MEAN_REVERSION_EXTREME):
        direction = "SHORT"
    else:
        return _no_setup(setup_type, True,
                          [f"RANGE regime confirmed but price is mid-range (position_in_range={pos}) -- not at an extreme yet"])

    mid = (support + resistance) / 2
    reasons = [f"{primary} regime with {regime['confidence']} confidence",
               f"price at range extreme (position_in_range={pos})"]
    counter = []
    entry_zone = {"low": support, "high": support + atr} if direction == "LONG" else {"low": resistance - atr, "high": resistance}
    invalidation = support - atr * SL_ATR_BUFFER_MULT if direction == "LONG" else resistance + atr * SL_ATR_BUFFER_MULT
    stop_loss = invalidation
    targets = [mid, resistance] if direction == "LONG" else [mid, support]

    confirmations, contradictions = 0, 0
    orderflow_confirmed = False
    if volume.get("available"):
        cvd_slope = _d(volume.get("cvd_slope_15m"))
        if cvd_slope is not None:
            exhausted = (direction == "LONG" and cvd_slope >= 0) or (direction == "SHORT" and cvd_slope <= 0)
            if exhausted:
                reasons.append("CVD slope shows selling/buying pressure easing at the extreme")
                confirmations += 1
                orderflow_confirmed = True
            else:
                counter.append("CVD slope still accelerating in the extreme's direction -- reversion not confirmed")
                contradictions += 1

    return {
        "setup_type": setup_type, "direction": direction,
        "entry_zone": entry_zone, "invalidation": invalidation, "stop_loss": stop_loss, "targets": targets,
        "reasons": reasons, "counterarguments": counter,
        "regime_compatible": True, "orderflow_confirmed": orderflow_confirmed,
        "price_only_mode": not volume.get("available", False),
        "_base_confidence": 0.50, "_confirmations": confirmations, "_contradictions": contradictions,
    }


# ── liquidation_reversal ─────────────────────────────────────────────────

def _liquidation_reversal(symbol: str, regime: dict, tf: Dict[str, dict], liquidations: dict) -> dict:
    setup_type = "liquidation_reversal"
    primary = regime["primary_regime"]
    if primary not in REGIME_COMPATIBILITY[setup_type]:
        return _no_setup(setup_type, False, [f"regime {primary} does not support a liquidation-reversal setup"])

    pa_15m = tf.get("15m") or {}
    if not pa_15m.get("available"):
        return _no_setup(setup_type, True, ["15m price-action unavailable -- cannot locate the cascade extreme"])

    direction = "LONG" if primary == "PANIC_LIQUIDATION_LONGS" else "SHORT"
    last_close = _d(pa_15m.get("last_close"))
    atr = _d(pa_15m.get("atr"))
    support = _d(pa_15m.get("support"))
    resistance = _d(pa_15m.get("resistance"))
    if None in (last_close, atr, support, resistance):
        return _no_setup(setup_type, True, ["insufficient 15m range/ATR history to define the cascade extreme"])

    reasons = [f"{primary} regime with {regime['confidence']} confidence",
               "contrarian reversal against the liquidation cascade"]
    counter = ["a cascade can resume and take out the recent extreme before reversing"]
    extreme = support if direction == "LONG" else resistance
    entry_zone = {"low": min(last_close, extreme), "high": max(last_close, extreme) + atr * LIQ_REVERSAL_ENTRY_ATR_MULT}
    invalidation = extreme - atr * SL_ATR_BUFFER_MULT if direction == "LONG" else extreme + atr * SL_ATR_BUFFER_MULT
    stop_loss = invalidation
    retrace = abs(resistance - support)
    targets = [last_close + retrace * Decimal(m) if direction == "LONG" else last_close - retrace * Decimal(m)
               for m in ("0.382", "0.618", "1.0")]

    confirmations = 1  # cascade_detected already gated the regime itself
    contradictions = 1  # the standing counterargument above
    orderflow_confirmed = False
    imbalance = _d(liquidations.get("imbalance_15m")) if liquidations.get("available") else None
    if imbalance is not None:
        strongly_one_sided = abs(imbalance) >= Decimal("0.6")
        if strongly_one_sided:
            reasons.append(f"liquidation imbalance {imbalance} strongly one-sided, consistent with a flush/exhaustion")
            confirmations += 1
            orderflow_confirmed = True

    return {
        "setup_type": setup_type, "direction": direction,
        "entry_zone": entry_zone, "invalidation": invalidation, "stop_loss": stop_loss, "targets": targets,
        "reasons": reasons, "counterarguments": counter,
        "regime_compatible": True, "orderflow_confirmed": orderflow_confirmed,
        "price_only_mode": False,
        "_base_confidence": 0.45, "_confirmations": confirmations, "_contradictions": contradictions,
    }


def detect_setups(
    symbol: str,
    as_of: dt.datetime,
    regime: dict,
    tf: Dict[str, dict],
    volume: dict,
    orderbook: dict,
    futures: dict,
    liquidations: dict,
) -> dict:
    """Evaluates all 4 setup types against one already-computed regime call
    (Warstwa 1's output) and the same feature dicts `feature_engine`
    produces. Pure function -- NEVER touches the DB or places an order.

    Most calls will correctly return NO_SETUP for 3 of the 4 types: a
    given moment's regime is compatible with at most one or two setup
    families by design (see REGIME_COMPATIBILITY)."""
    regime_confidence = float(regime.get("confidence", 0.0))

    raw = {
        "trend_pullback": _trend_pullback(symbol, regime, tf, volume),
        "breakout": _breakout(symbol, regime, tf, volume, futures),
        "mean_reversion": _mean_reversion(symbol, regime, tf, volume),
        "liquidation_reversal": _liquidation_reversal(symbol, regime, tf, liquidations),
    }
    setups = {}
    for name, decision in raw.items():
        if decision["direction"] == "NO_SETUP":
            setups[name] = decision
        else:
            setups[name] = _finalize(decision, regime_confidence)

    return {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "regime_context": {"primary_regime": regime["primary_regime"], "confidence": regime["confidence"],
                            "secondary_flags": regime.get("secondary_flags", [])},
        "setups": setups,
    }
