"""Multi-timeframe Market Regime Classifier — v2 (research layer, "Warstwa 1").

Deterministic, rule-based, NO machine learning and NO price prediction:
this describes CURRENT market conditions for later strategy selection, it
does not forecast direction. Coexists with (does not replace) the simpler,
single-timeframe classifier already in `regime_classifier.py`, which stays
wired into the live Block 2 Strategy Engine exactly as-is -- this module
is additive research-track code, not a swap-in replacement, until it has
been separately validated and someone deliberately wires it in.

All thresholds marked "HYPOTHESIS" are reasonable starting guesses, not
calibrated on this project's own data yet -- validate/tune them using the
historical backtest in scripts/research/audit_regime_classifier.py before
trusting them beyond directional research.

Pure functions only: no DB/network access, so this file is unit-testable
in complete isolation (mirrors core/analysis/feature_engine.py's pattern).
Inputs are the same shaped dicts feature_engine.compute_*_features already
produce, so the DB-touching assembly layer (core/services/regime_service_v2.py)
can build them by calling feature_engine directly -- zero reimplementation.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

PRIMARY_REGIMES = (
    "TREND_UP", "TREND_DOWN", "RANGE", "BREAKOUT_UP", "BREAKOUT_DOWN",
    "SQUEEZE", "HIGH_VOLATILITY", "PANIC_LIQUIDATION_LONGS",
    "PANIC_LIQUIDATION_SHORTS", "LOW_LIQUIDITY", "UNSTABLE_DATA", "NO_EDGE",
)

# Regimes urgent enough to override hysteresis and switch immediately.
IMMEDIATE_OVERRIDE_REGIMES = {
    "PANIC_LIQUIDATION_LONGS", "PANIC_LIQUIDATION_SHORTS",
    "BREAKOUT_UP", "BREAKOUT_DOWN", "UNSTABLE_DATA",
}

# --- HYPOTHESIS thresholds: reasonable starting points, not yet tuned ---
HIGH_VOL_ATR_PCT = Decimal("3.0")
LOW_VOL_ATR_PCT = Decimal("0.5")
LOW_LIQUIDITY_SPREAD_BPS = Decimal("15")
UNSTABLE_SPREAD_BPS = Decimal("40")
MIN_DATA_QUALITY_SCORE = 50
CASCADE_MIN_IMBALANCE = Decimal("0.5")
# Matches orderflow_confirmation_v2.FUNDING_EXTREME_ABS -- same "is this
# funding actually extreme" bar used consistently across layers.
FUNDING_COUNTERARGUMENT_THRESHOLD = Decimal("0.0005")
BREAKOUT_POSITION_HIGH = Decimal("0.9")
BREAKOUT_POSITION_LOW = Decimal("0.1")
VOLUME_ZSCORE_CONFIRM = Decimal("1.0")
OI_EXPANSION_PCT = Decimal("5")
CONFIDENCE_FLOOR_FOR_NO_EDGE = 0.35
HYSTERESIS_CONFIRM_TICKS = 2


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _trend_direction(pa: dict) -> Optional[str]:
    """'UP'/'DOWN'/None from one timeframe's price_action feature dict."""
    if not pa or not pa.get("available"):
        return None
    structure = pa.get("structure")
    ema20, ema50, ema200 = _d(pa.get("ema20")), _d(pa.get("ema50")), _d(pa.get("ema200"))
    last_close = _d(pa.get("last_close"))
    if structure == "HH_HL" and ema20 is not None and ema50 is not None and ema20 > ema50 \
            and (ema200 is None or (last_close is not None and last_close > ema200)):
        return "UP"
    if structure == "LH_LL" and ema20 is not None and ema50 is not None and ema20 < ema50 \
            and (ema200 is None or (last_close is not None and last_close < ema200)):
        return "DOWN"
    return None


def _reasons_for_oi(futures: dict, direction: str) -> Tuple[List[str], List[str]]:
    """OI alone never confirms a trend -- always cross-checked against the
    (price, OI) quadrant already computed by feature_engine."""
    reasons, counter = [], []
    if not futures.get("available"):
        return reasons, counter
    relation = futures.get("price_to_oi_relation")
    if relation == "PRICE_UP_OI_UP" and direction == "UP":
        reasons.append("open interest rising alongside price -- new long positioning")
    elif relation == "PRICE_DOWN_OI_UP" and direction == "DOWN":
        reasons.append("open interest rising alongside the decline -- new short positioning")
    elif relation == "PRICE_UP_OI_DOWN" and direction == "UP":
        counter.append("open interest falling despite rising price -- likely short covering, not new demand")
    elif relation == "PRICE_DOWN_OI_DOWN" and direction == "DOWN":
        counter.append("open interest falling alongside the decline -- likely position closing, not new supply")
    return reasons, counter


def _reasons_for_cvd(volume: dict, direction: str) -> Tuple[List[str], List[str]]:
    reasons, counter = [], []
    if not volume.get("available"):
        return reasons, counter
    cvd_slope = _d(volume.get("cvd_slope_15m"))
    if cvd_slope is None:
        return reasons, counter
    if direction == "UP" and cvd_slope > 0:
        reasons.append("positive CVD slope confirms buy-side aggression")
    elif direction == "DOWN" and cvd_slope < 0:
        reasons.append("negative CVD slope confirms sell-side aggression")
    elif direction == "UP" and cvd_slope < 0:
        counter.append("CVD slope is negative despite rising price -- a divergence")
    elif direction == "DOWN" and cvd_slope > 0:
        counter.append("CVD slope is positive despite falling price -- a divergence")
    return reasons, counter


def _reasons_for_funding(futures: dict, direction: str) -> Tuple[List[str], List[str]]:
    """Funding is context only, never a standalone signal -- flags
    crowding (squeeze) risk AGAINST the current position only when
    funding is crowded WITH the trade's own direction (positive funding
    while LONG, negative funding while SHORT): that's when OUR side is
    the one paying and exposed to a squeeze if price reverses.

    Funding crowded AGAINST the trade's direction (e.g. negative funding
    while LONG) means the OPPOSITE side is paying -- if anything that's a
    tailwind (their squeeze risk, not ours), never a counterargument to
    our own position. An earlier version of this function had the sign
    backwards, flagging exactly the favorable case as a risk; fixed here.
    A magnitude floor (FUNDING_COUNTERARGUMENT_THRESHOLD) avoids firing on
    routine, non-extreme funding that most trending markets carry."""
    reasons, counter = [], []
    if not futures.get("available"):
        return reasons, counter
    funding = _d(futures.get("funding_rate"))
    if funding is None or abs(funding) < FUNDING_COUNTERARGUMENT_THRESHOLD:
        return reasons, counter
    if direction == "UP" and funding > 0:
        counter.append(f"funding {funding} positive while price rises -- longs are crowded and paying, squeeze risk against this long position")
    elif direction == "DOWN" and funding < 0:
        counter.append(f"funding {funding} negative while price falls -- shorts are crowded and paying, squeeze risk against this short position")
    return reasons, counter


def _calibrate_confidence(base: float, confirmations: int, contradictions: int, data_quality_score) -> float:
    score = base + 0.05 * confirmations - 0.08 * contradictions
    score *= max(0.0, min(1.0, float(data_quality_score) / 100.0))
    return round(max(0.05, min(0.93, score)), 2)


def _liquidation_regime(liquidations: dict, price_action_15m: dict) -> Optional[dict]:
    if not liquidations.get("available") or not liquidations.get("cascade_detected"):
        return None
    imbalance = _d(liquidations.get("imbalance_15m"))
    if imbalance is None:
        return None
    pct_change = _d(price_action_15m.get("pct_change")) if price_action_15m.get("available") else None

    if imbalance <= -CASCADE_MIN_IMBALANCE:
        reasons = [f"liquidation cascade detected, 15m imbalance={imbalance} (long-dominated)"]
        counter = []
        if pct_change is not None and pct_change < 0:
            reasons.append(f"price confirms with a {pct_change}% 15m move down")
        else:
            counter.append("price action does not yet confirm the direction implied by the cascade")
        return {"regime": "PANIC_LIQUIDATION_LONGS", "reasons": reasons, "counterarguments": counter, "base_confidence": 0.65}

    if imbalance >= CASCADE_MIN_IMBALANCE:
        reasons = [f"liquidation cascade detected, 15m imbalance={imbalance} (short-dominated)"]
        counter = []
        if pct_change is not None and pct_change > 0:
            reasons.append(f"price confirms with a {pct_change}% 15m move up")
        else:
            counter.append("price action does not yet confirm the direction implied by the cascade")
        return {"regime": "PANIC_LIQUIDATION_SHORTS", "reasons": reasons, "counterarguments": counter, "base_confidence": 0.65}

    return None


def _data_quality_check(tf: Dict[str, dict], data_quality: dict) -> Optional[dict]:
    # 15m is transition-detection-only per the architecture (see module
    # docstring / squeeze+breakout+trend logic below, which already treat it
    # as optional via `.get("available")` guards) -- it must NOT be a hard
    # requirement here. Only 4h (main regime) and 1h (current context) gate
    # UNSTABLE_DATA; a missing 15m read is surfaced as a warning instead,
    # by the caller.
    missing = [k for k in ("4h", "1h") if not (tf.get(k) or {}).get("available")]
    dq_score = data_quality.get("data_quality_score", 0)
    stale = data_quality.get("stale_fields", [])
    if missing or dq_score < MIN_DATA_QUALITY_SCORE or not data_quality.get("is_tradeable", False):
        reasons = []
        if missing:
            reasons.append(f"missing price-action data for timeframe(s): {missing}")
        if stale:
            reasons.append(f"stale data sources: {stale}")
        if dq_score < MIN_DATA_QUALITY_SCORE:
            reasons.append(f"data_quality_score {dq_score} below floor {MIN_DATA_QUALITY_SCORE}")
        return {"regime": "UNSTABLE_DATA", "reasons": reasons or ["data quality gate failed"],
                "counterarguments": [], "base_confidence": 0.3}
    return None


def classify_from_features(
    symbol: str,
    as_of: dt.datetime,
    tf: Dict[str, dict],
    volume: dict,
    orderbook: dict,
    futures: dict,
    liquidations: dict,
    data_quality: dict,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
) -> dict:
    """Classify the current market regime for one symbol at one point in
    time. Pure function -- caller assembles `tf`/`volume`/`orderbook`/
    `futures`/`liquidations`/`data_quality` from already-persisted data
    (same shapes feature_engine.compute_*_features already produce)."""

    secondary_flags: List[str] = []
    warnings: List[str] = list(data_quality.get("missing_fields", []) or [])
    if not (tf.get("15m") or {}).get("available"):
        warnings.append("15m price-action unavailable -- transition detection degraded, primary regime unaffected")

    decision = _data_quality_check(tf, data_quality)

    if decision is None:
        pa_15m = tf.get("15m") or {}
        pa_1h = tf.get("1h") or {}
        pa_4h = tf.get("4h") or {}

        spread_bps = _d(orderbook.get("spread_bps")) if orderbook.get("available") else None
        if spread_bps is not None and spread_bps >= LOW_LIQUIDITY_SPREAD_BPS:
            severity = "extreme" if spread_bps >= UNSTABLE_SPREAD_BPS else "thin"
            decision = {
                "regime": "LOW_LIQUIDITY",
                "reasons": [f"order-book spread {spread_bps}bps indicates {severity} liquidity"],
                "counterarguments": [], "base_confidence": 0.65 if severity == "extreme" else 0.55,
            }

        liq_decision = _liquidation_regime(liquidations, pa_15m)
        if liq_decision is not None:
            decision = liq_decision  # panic liquidation overrides a plain liquidity read too

        if decision is None:
            atr_15m = _d(pa_15m.get("atr_pct")) if pa_15m.get("available") else None
            struct_1h = pa_1h.get("structure")
            struct_4h = pa_4h.get("structure")
            oi_1h_pct = _d(futures.get("oi_change_1h_pct")) if futures.get("available") else None
            cvd_slope = _d(volume.get("cvd_slope_15m")) if volume.get("available") else None
            volume_z = _d(volume.get("volume_zscore")) if volume.get("available") else None
            pos_15m = _d(pa_15m.get("position_in_range")) if pa_15m.get("available") else None

            # Squeeze: compressed range + low vol + OI still building + flat CVD
            is_compressed = struct_1h == "CONTRACTING" or struct_4h == "CONTRACTING"
            is_low_vol = atr_15m is not None and atr_15m <= LOW_VOL_ATR_PCT
            oi_building = oi_1h_pct is not None and oi_1h_pct > 0
            # A missing CVD reading is NOT evidence of flatness -- it's an
            # absence of evidence. Squeeze must be confirmed by an actually
            # observed near-zero slope, not assumed from missing data.
            cvd_flat = cvd_slope is not None and abs(cvd_slope) < Decimal("1")
            if is_compressed and is_low_vol and oi_building and cvd_flat:
                decision = {
                    "regime": "SQUEEZE",
                    "reasons": [
                        "range compression on 1h/4h structure",
                        f"15m ATR% {atr_15m} at/below the compression threshold",
                        f"open interest building (+{oi_1h_pct}% over 1h) without a directional resolution",
                    ],
                    "counterarguments": ["compression can resolve in either direction without warning"],
                    "base_confidence": 0.5,
                }

            # Breakout up/down: expanding structure + edge-of-range position
            # + volume/CVD/OI confirmation. Missing confirmation demotes
            # this to a false-breakout warning, falls through below.
            attempted_failed_breakout = False
            if decision is None:
                expanding = struct_1h == "EXPANDING" or struct_4h == "EXPANDING"
                if expanding and pos_15m is not None:
                    up_break = pos_15m >= BREAKOUT_POSITION_HIGH
                    down_break = pos_15m <= BREAKOUT_POSITION_LOW
                    confirmations = sum([
                        volume_z is not None and volume_z >= VOLUME_ZSCORE_CONFIRM,
                        cvd_slope is not None and ((up_break and cvd_slope > 0) or (down_break and cvd_slope < 0)),
                        oi_1h_pct is not None and oi_1h_pct > 0,
                    ])
                    if up_break and confirmations >= 2:
                        decision = {"regime": "BREAKOUT_UP",
                                    "reasons": ["expanding structure with price near the top of its recent range",
                                                f"{confirmations}/3 confirming signals (volume, CVD, OI)"],
                                    "counterarguments": [], "base_confidence": 0.55}
                    elif down_break and confirmations >= 2:
                        decision = {"regime": "BREAKOUT_DOWN",
                                    "reasons": ["expanding structure with price near the bottom of its recent range",
                                                f"{confirmations}/3 confirming signals (volume, CVD, OI)"],
                                    "counterarguments": [], "base_confidence": 0.55}
                    elif up_break or down_break:
                        warnings.append("range break detected without volume/CVD/OI confirmation (possible false breakout)")
                        attempted_failed_breakout = True

            # Trend: require 4h AND 1h agreement; 15m only flags a transition.
            if decision is None:
                dir_4h = _trend_direction(pa_4h)
                dir_1h = _trend_direction(pa_1h)
                dir_15m = _trend_direction(pa_15m)
                if dir_4h is not None and dir_4h == dir_1h:
                    oi_r, oi_c = _reasons_for_oi(futures, dir_4h)
                    cvd_r, cvd_c = _reasons_for_cvd(volume, dir_4h)
                    fund_r, fund_c = _reasons_for_funding(futures, dir_4h)
                    reasons = [f"4h and 1h structure both agree on a {dir_4h.lower()}trend"] + oi_r + cvd_r + fund_r
                    counter = oi_c + cvd_c + fund_c
                    if dir_15m is not None and dir_15m != dir_4h:
                        counter.append("15m structure has started diverging from the higher-timeframe trend")
                    decision = {
                        "regime": "TREND_UP" if dir_4h == "UP" else "TREND_DOWN",
                        "reasons": reasons, "counterarguments": counter, "base_confidence": 0.55,
                        "_confirmations": len(oi_r) + len(cvd_r), "_contradictions": len(oi_c) + len(cvd_c) + len(fund_c),
                    }
                elif dir_4h is not None and dir_1h is not None and dir_4h != dir_1h:
                    decision = {
                        "regime": "NO_EDGE",
                        "reasons": [f"4h trend ({dir_4h}) disagrees with 1h trend ({dir_1h})"],
                        "counterarguments": [], "base_confidence": 0.4,
                    }

            if decision is None and atr_15m is not None and atr_15m >= HIGH_VOL_ATR_PCT:
                decision = {"regime": "HIGH_VOLATILITY",
                            "reasons": [f"15m ATR% {atr_15m} at/above the high-volatility threshold with no clear structure"],
                            "counterarguments": [], "base_confidence": 0.5}
            elif atr_15m is not None and atr_15m >= HIGH_VOL_ATR_PCT:
                secondary_flags.append("HIGH_VOLATILITY")

            if oi_1h_pct is not None and oi_1h_pct >= OI_EXPANSION_PCT:
                secondary_flags.append("OI_EXPANSION")
            elif oi_1h_pct is not None and oi_1h_pct <= -OI_EXPANSION_PCT:
                secondary_flags.append("OI_CONTRACTION")

            if decision is None:
                # RANGE must be earned by positive evidence of consolidation,
                # not assumed as the leftover bucket when nothing else fired.
                # "Nothing matched" can just as easily mean "the evidence is
                # ambiguous/insufficient" -- that's NO_EDGE's job, not RANGE's.
                non_expanding = struct_4h != "EXPANDING" and struct_1h != "EXPANDING"
                atr_ref = atr_15m
                if atr_ref is None and pa_1h.get("available"):
                    atr_ref = _d(pa_1h.get("atr_pct"))
                moderate_vol = atr_ref is not None and atr_ref < HIGH_VOL_ATR_PCT
                bounded_structure = struct_4h in ("RANGE", "UNKNOWN", "CONTRACTING") or \
                    struct_1h in ("RANGE", "UNKNOWN", "CONTRACTING")
                # A rejected breakout attempt (price pushed to the range edge
                # but failed to get volume/CVD/OI confirmation) is itself
                # positive evidence of range-bound behavior, even though
                # structure was technically "EXPANDING" going into it.
                has_range_evidence = attempted_failed_breakout or (non_expanding and (moderate_vol or bounded_structure))
                if has_range_evidence:
                    decision = {"regime": "RANGE",
                                "reasons": ["no directional trend, breakout, or squeeze condition met",
                                            "structure/ATR consistent with bounded, non-expanding conditions"],
                                "counterarguments": [], "base_confidence": 0.45}
                else:
                    decision = {"regime": "NO_EDGE",
                                "reasons": ["no positive evidence of trend, breakout, squeeze, or consolidation -- ambiguous market state"],
                                "counterarguments": [], "base_confidence": 0.35}

    confirmations = decision.pop("_confirmations", 0)
    contradictions = decision.pop("_contradictions", len(decision.get("counterarguments", [])))
    dq_score = data_quality.get("data_quality_score", 100)
    confidence = _calibrate_confidence(decision["base_confidence"], confirmations, contradictions, dq_score)

    if decision["regime"] != "UNSTABLE_DATA" and confidence < CONFIDENCE_FLOOR_FOR_NO_EDGE:
        prior_regime = decision["regime"]
        decision = {
            "regime": "NO_EDGE",
            "reasons": [f"calibrated confidence {confidence} below the actionable floor {CONFIDENCE_FLOOR_FOR_NO_EDGE}"],
            "counterarguments": [f"best candidate before the floor check was {prior_regime}"] + decision.get("reasons", []),
        }
        confidence = max(confidence, 0.2)

    if btc_regime is not None and symbol != "BTCUSDT" and decision["regime"] in ("TREND_UP", "TREND_DOWN"):
        symbol_dir = "UP" if decision["regime"] == "TREND_UP" else "DOWN"
        btc_dir = "UP" if btc_regime == "TREND_UP" else ("DOWN" if btc_regime == "TREND_DOWN" else None)
        if btc_dir is not None and btc_dir != symbol_dir:
            decision.setdefault("counterarguments", []).append(
                f"diverges from BTC's own regime ({btc_regime}) -- treat with extra caution"
            )

    final_regime, updated_state, transitioning = apply_hysteresis(decision["regime"], confidence, as_of, previous_state)
    if transitioning:
        secondary_flags.append("REGIME_TRANSITION_PENDING")

    return {
        "symbol": symbol,
        "primary_regime": final_regime,
        "secondary_flags": secondary_flags,
        "confidence": confidence,
        "data_quality": round(float(data_quality.get("data_quality_score", 0)) / 100, 2),
        "reasons": decision.get("reasons", []),
        "counterarguments": decision.get("counterarguments", []),
        "timestamp": as_of.isoformat(),
        "intervals_used": ["4h", "1h", "15m"],
        "warnings": warnings,
        "_state": updated_state,
    }


def apply_hysteresis(
    candidate_regime: str,
    confidence: float,
    as_of: dt.datetime,
    previous_state: Optional[dict],
) -> Tuple[str, dict, bool]:
    """Prevents chattering: a non-override regime must be the classifier's
    raw output for HYSTERESIS_CONFIRM_TICKS consecutive calls before it
    replaces the currently-confirmed regime. PANIC_LIQUIDATION_*,
    BREAKOUT_*, and UNSTABLE_DATA bypass this and switch immediately.

    `previous_state` is whatever this function returned last time (the
    caller threads it through -- persisted to JSON between live runs, or
    kept in memory during a historical backtest loop)."""
    if previous_state is None:
        return candidate_regime, {
            "confirmed_regime": candidate_regime, "confirmed_at": as_of.isoformat(),
            "pending_regime": None, "pending_count": 0,
        }, False

    confirmed = previous_state.get("confirmed_regime")
    if candidate_regime == confirmed:
        return confirmed, {**previous_state, "pending_regime": None, "pending_count": 0}, False

    if candidate_regime in IMMEDIATE_OVERRIDE_REGIMES:
        return candidate_regime, {
            "confirmed_regime": candidate_regime, "confirmed_at": as_of.isoformat(),
            "pending_regime": None, "pending_count": 0,
        }, False

    pending = previous_state.get("pending_regime")
    pending_count = previous_state.get("pending_count", 0)
    if pending == candidate_regime:
        pending_count += 1
    else:
        pending, pending_count = candidate_regime, 1

    if pending_count >= HYSTERESIS_CONFIRM_TICKS:
        return candidate_regime, {
            "confirmed_regime": candidate_regime, "confirmed_at": as_of.isoformat(),
            "pending_regime": None, "pending_count": 0,
        }, False

    return confirmed, {**previous_state, "pending_regime": pending, "pending_count": pending_count}, True
