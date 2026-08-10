"""Risk Manager -- Warstwa 4 (research layer).

Takes the existing regime -> setup -> order-flow-confirmation chain
(Warstwa 1-3) and decides ONE of: APPROVED, APPROVED_REDUCED_SIZE,
WAIT_FOR_CONFIRMATION, REJECTED. This module NEVER flips LONG to SHORT,
NEVER invents a new setup, and NEVER widens a stop loss just to make a
setup pass -- it only approves, shrinks, holds, or rejects the setup
Warstwa 2 already produced. It computes sizing and records a decision; it
does NOT place, modify, or cancel any order (paper or real) -- that is
explicitly Warstwa 5's job.

Stop loss is taken AS-IS from Warstwa 2's `stop_loss` (itself already
derived from invalidation +/- ATR buffer -- see setup_detector_v2). This
module never recomputes or relaxes it; if the given SL implies too much
risk or too weak a reward:risk, the setup is REJECTED, not patched.

Confirmation-status -> decision mapping (the core policy):
  CONFIRMED          -> APPROVED (full size, subject to portfolio limits)
  WEAK_CONFIRMATION  -> APPROVED_REDUCED_SIZE
  NEUTRAL            -> APPROVED_REDUCED_SIZE if setup confidence clears
                        NEUTRAL_MIN_CONFIDENCE_FOR_ENTRY, else
                        WAIT_FOR_CONFIRMATION
  CONTRADICTED       -> WAIT_FOR_CONFIRMATION (never an automatic entry;
                        the setup is NOT deleted -- for mean_reversion in
                        particular this correctly means "price is at the
                        extreme, order flow just hasn't turned yet" --
                        `activation_conditions` records what would need to
                        change before this could re-evaluate to APPROVED)
  INSUFFICIENT_DATA  -> APPROVED_REDUCED_SIZE (heavily reduced) only if
                        the setup is itself price-only AND base data
                        quality is decent; REJECTED otherwise
  NOT_APPLICABLE     -> REJECTED (nothing to approve)

Kill switches (checked first, force REJECTED regardless of the above):
stale/untradeable regime data, spread too wide, reward:risk below
minimum, missing/invalid SL or entry, max open positions reached,
daily/weekly loss limit breached, total/symbol/correlated-group exposure
breached with no remaining room, a detected BTC-regime whiplash flag.

Pure functions only: no DB/network access, no order execution.

All thresholds and the fee/slippage model are marked HYPOTHESIS --
reasonable starting points, not tuned against this project's own data,
and definitely not calibrated against real Bybit fee schedules or fill
data yet.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional

RISK_DECISIONS = ("APPROVED", "APPROVED_REDUCED_SIZE", "WAIT_FOR_CONFIRMATION", "REJECTED")

RISK_CONFIG_VERSION = "1.0"

DEFAULT_RISK_CONFIG: dict = {
    "version": RISK_CONFIG_VERSION,
    "paper_capital": Decimal("10000"),
    "base_risk_pct": Decimal("0.5"),
    "max_risk_pct": Decimal("1.0"),
    "max_daily_loss_pct": Decimal("3.0"),
    "max_weekly_loss_pct": Decimal("8.0"),
    "max_open_positions": 6,
    "max_total_exposure_pct": Decimal("50.0"),
    "max_symbol_exposure_pct": Decimal("15.0"),
    # HYPOTHESIS: no real correlation matrix has been computed yet, so
    # every non-BTCUSDT symbol is treated as one correlated group. Refine
    # once actual historical price correlation is available.
    "max_correlated_alt_exposure_pct": Decimal("30.0"),
    "max_directional_concentration_pct": Decimal("70.0"),
    "min_reward_to_risk": Decimal("1.5"),
    "max_spread_bps_for_entry": Decimal("15"),
    "min_confidence_for_full_size": Decimal("0.5"),
    "neutral_min_confidence_for_entry": Decimal("0.40"),
    # HYPOTHESIS fee/slippage model -- Bybit-like taker fee, not calibrated
    # against real fills.
    "taker_fee_rate_pct": Decimal("0.055"),
    "slippage_bps_per_unit_spread": Decimal("0.5"),
}

_STATUS_SIZE_MULTIPLIER = {
    "CONFIRMED": Decimal("1.0"),
    "WEAK_CONFIRMATION": Decimal("0.5"),
    "NEUTRAL": Decimal("0.4"),
    "CONTRADICTED": Decimal("0.0"),
    "INSUFFICIENT_DATA": Decimal("0.25"),
}


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _confidence_scale(confidence: Decimal, min_conf: Decimal) -> Decimal:
    """1.0x at/above `min_conf`, floors at 0.25x for very low confidence,
    linear in between. Confidence never INCREASES size beyond 1.0x on its
    own -- it only ever throttles down."""
    if confidence >= min_conf:
        return Decimal("1.0")
    floor_conf = Decimal("0.30")
    if confidence <= floor_conf:
        return Decimal("0.25")
    span = min_conf - floor_conf
    frac = (confidence - floor_conf) / span if span > 0 else Decimal("1")
    return Decimal("0.25") + frac * Decimal("0.75")


def _no_decision_result(symbol: str, setup: dict, config: dict, reason: str) -> dict:
    return {
        "symbol": symbol, "decision": "REJECTED", "direction": setup.get("direction"),
        "reference_price": None, "worst_case_entry": None, "entry_zone": None, "stop_loss": None, "sl_distance_pct": None,
        "risk_amount": Decimal("0"), "position_size": Decimal("0"), "position_value": Decimal("0"),
        "leverage_estimate": None, "targets": [], "reward_to_risk": [], "expected_fees": Decimal("0"),
        "estimated_slippage_bps": None, "reasons": [], "rejection_reasons": [reason],
        "activation_conditions": [], "portfolio_limits_used": {}, "config_version": config["version"],
    }


def _check_kill_switches(
    symbol: str, regime: dict, setup: dict, confirmation: dict, orderbook: dict, data_quality: dict,
    portfolio_state: dict, config: dict,
) -> Optional[str]:
    if not data_quality.get("is_tradeable", False):
        return "kill switch: regime data quality gate failed (stale/missing critical source)"
    if regime.get("primary_regime") == "UNSTABLE_DATA":
        return "kill switch: regime itself is UNSTABLE_DATA -- collector likely degraded"
    spread_bps = _d(orderbook.get("spread_bps")) if orderbook.get("available") else None
    if spread_bps is not None and spread_bps >= config["max_spread_bps_for_entry"]:
        return f"kill switch: spread {spread_bps}bps at/above max_spread_bps_for_entry {config['max_spread_bps_for_entry']}"
    if portfolio_state.get("daily_realized_pnl_pct", Decimal("0")) <= -config["max_daily_loss_pct"]:
        return f"kill switch: daily loss limit breached ({portfolio_state.get('daily_realized_pnl_pct')}% <= -{config['max_daily_loss_pct']}%)"
    if portfolio_state.get("weekly_realized_pnl_pct", Decimal("0")) <= -config["max_weekly_loss_pct"]:
        return f"kill switch: weekly loss limit breached ({portfolio_state.get('weekly_realized_pnl_pct')}% <= -{config['max_weekly_loss_pct']}%)"
    if portfolio_state.get("btc_regime_flip_detected", False):
        return "kill switch: BTC regime whiplash detected -- pausing new entries until it stabilizes"
    open_positions = portfolio_state.get("open_positions", [])
    if len(open_positions) >= config["max_open_positions"]:
        return f"kill switch: max_open_positions {config['max_open_positions']} reached"
    if any(p["symbol"] == symbol for p in open_positions):
        return "kill switch: a position (or already-approved signal) for this symbol already exists this run -- not treated as an independent signal"
    return None


def _correlated_group(symbol: str, config: dict) -> Optional[str]:
    return None if symbol == "BTCUSDT" else config.get("correlated_group", "ALTS")


def _check_portfolio_limits(symbol: str, direction: str, position_value: Decimal, risk_amount: Decimal,
                             portfolio_state: dict, config: dict) -> dict:
    """Returns {'hard_reject': bool, 'reason': str|None, 'reduced_to_value': Decimal|None}.
    Never traits N similar alt LONGs as independent -- all non-BTCUSDT
    exposure is summed into one correlated-group figure regardless of
    which alt it's on."""
    capital = config["paper_capital"]
    open_positions = portfolio_state.get("open_positions", [])
    total_exposure = sum((p["position_value"] for p in open_positions), Decimal("0"))
    symbol_exposure = sum((p["position_value"] for p in open_positions if p["symbol"] == symbol), Decimal("0"))
    group = _correlated_group(symbol, config)
    group_exposure = sum((p["position_value"] for p in open_positions if _correlated_group(p["symbol"], config) == group), Decimal("0")) if group else Decimal("0")
    long_exposure = sum((p["position_value"] for p in open_positions if p["direction"] == "LONG"), Decimal("0"))
    short_exposure = sum((p["position_value"] for p in open_positions if p["direction"] == "SHORT"), Decimal("0"))

    max_total = capital * config["max_total_exposure_pct"] / 100
    max_symbol = capital * config["max_symbol_exposure_pct"] / 100
    max_group = capital * config["max_correlated_alt_exposure_pct"] / 100
    max_direction = capital * config["max_directional_concentration_pct"] / 100

    room_total = max_total - total_exposure
    room_symbol = max_symbol - symbol_exposure
    room_group = (max_group - group_exposure) if group else max_total
    dir_exposure_after = (long_exposure if direction == "LONG" else short_exposure) + position_value
    room_direction = max_direction - (long_exposure if direction == "LONG" else short_exposure)

    room_by_limit = {
        "total_exposure": room_total, "symbol_exposure": room_symbol,
        "correlated_group_exposure": room_group, "directional_concentration": room_direction,
    }
    room = min(room_by_limit.values())
    # Name the SPECIFIC binding constraint(s) rather than a generic message
    # -- with four independent caps, "room=0" alone doesn't say which one
    # actually fired, which matters for anyone reading the rejection later.
    binding = sorted(name for name, r in room_by_limit.items() if r == room)
    limits_used = {
        "total_exposure_before": total_exposure, "max_total_exposure": max_total,
        "symbol_exposure_before": symbol_exposure, "max_symbol_exposure": max_symbol,
        "correlated_group": group, "group_exposure_before": group_exposure, "max_group_exposure": max_group,
        "directional_exposure_before": long_exposure if direction == "LONG" else short_exposure,
        "max_directional_exposure": max_direction,
        "binding_limit": binding,
    }
    if room <= 0:
        return {"hard_reject": True, "reason": f"portfolio limit reached: {', '.join(binding)} (room={room})",
                "reduced_to_value": None, "limits_used": limits_used}
    if position_value > room:
        return {"hard_reject": False, "reason": f"position reduced to fit remaining room on {', '.join(binding)} ({room} of requested {position_value})",
                "reduced_to_value": room, "limits_used": limits_used}
    return {"hard_reject": False, "reason": None, "reduced_to_value": None, "limits_used": limits_used}


def _build_activation_conditions(confirmation: dict, setup: dict) -> List[str]:
    conditions = []
    for dim in confirmation.get("dimensions", []):
        if dim["read"] == "CONTRADICTS" and not dim.get("already_used"):
            conditions.append(f"{dim['dimension']} must stop contradicting (currently: {dim['detail']})")
    if not conditions:
        conditions.append("at least one new independent order-flow confirmation must appear "
                           "(net_independent_score must rise above 0)")
    conditions.append(f"price must remain within/near entry_zone {setup.get('entry_zone')} for this setup to still apply")
    return conditions


def evaluate_risk(
    symbol: str,
    as_of: dt.datetime,
    regime: dict,
    setup: dict,
    confirmation: dict,
    orderbook: dict,
    data_quality: dict,
    portfolio_state: Optional[dict] = None,
    config: Optional[dict] = None,
) -> dict:
    """Evaluates ONE setup's risk. `portfolio_state` defaults to an empty
    book (no open positions, zero realized PnL) -- callers building up a
    running book across multiple symbols in one pass should pass the
    accumulated state forward themselves; this function never mutates it."""
    config = config or DEFAULT_RISK_CONFIG
    portfolio_state = portfolio_state or {"open_positions": [], "daily_realized_pnl_pct": Decimal("0"),
                                           "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False}
    direction = setup.get("direction")
    if direction not in ("LONG", "SHORT"):
        return _no_decision_result(symbol, setup, config, "no active LONG/SHORT setup to evaluate")

    kill_reason = _check_kill_switches(symbol, regime, setup, confirmation, orderbook, data_quality, portfolio_state, config)
    if kill_reason:
        r = _no_decision_result(symbol, setup, config, kill_reason)
        r["direction"] = direction
        return r

    entry_zone = setup.get("entry_zone")
    stop_loss = _d(setup.get("stop_loss"))
    targets = [_d(t) for t in (setup.get("targets") or [])]
    if not entry_zone or stop_loss is None or not targets or any(t is None for t in targets):
        r = _no_decision_result(symbol, setup, config, "missing or invalid SL/entry_zone/targets from setup -- cannot size a position")
        r["direction"] = direction
        return r

    zone_low, zone_high = _d(entry_zone["low"]), _d(entry_zone["high"])
    reference_price = (zone_low + zone_high) / 2  # midpoint -- reported for context/display only
    # Sizing and reward:risk are computed from the WORST price achievable
    # anywhere in entry_zone, not the midpoint. A fill can legitimately
    # land anywhere in the zone; sizing off the midpoint understated real
    # risk by up to ~60% when the actual fill landed near the far edge
    # (confirmed against a live LTCUSDT/SUIUSDT signal pair -- reported
    # risk_amount of $6.38 vs. a genuine $2.71-$10.07 range depending on
    # fill location). Worst-case entry = the zone edge farthest from SL
    # (highest price for a LONG, lowest for a SHORT) -- sizing off it
    # guarantees realized risk never exceeds the stated risk_amount,
    # only ever comes in safer than planned.
    worst_case_entry = zone_high if direction == "LONG" else zone_low
    sl_distance = abs(worst_case_entry - stop_loss)
    if sl_distance <= 0 or (direction == "LONG" and stop_loss >= zone_low) or (direction == "SHORT" and stop_loss <= zone_high):
        r = _no_decision_result(symbol, setup, config, f"invalid SL: {stop_loss} is not on the correct side of entry_zone [{zone_low}, {zone_high}] for a {direction}")
        r["direction"] = direction
        return r
    sl_distance_pct = sl_distance / worst_case_entry * 100

    reward_to_risk = [round(abs(t - worst_case_entry) / sl_distance, 2) for t in targets]
    if reward_to_risk[0] < config["min_reward_to_risk"]:
        r = _no_decision_result(symbol, setup, config,
                                 f"reward:risk {reward_to_risk[0]} on TP1 below minimum {config['min_reward_to_risk']} -- SL is not widened to force this through")
        r["direction"] = direction
        r["reference_price"] = reference_price
        r["worst_case_entry"] = worst_case_entry
        r["stop_loss"] = stop_loss
        r["sl_distance_pct"] = round(sl_distance_pct, 3)
        r["targets"] = targets
        r["reward_to_risk"] = reward_to_risk
        return r

    status = confirmation.get("status", "NOT_APPLICABLE")
    reasons: List[str] = []
    rejection_reasons: List[str] = []
    activation_conditions: List[str] = []

    if status == "CONFIRMED":
        decision = "APPROVED"
        reasons.append("order-flow CONFIRMED the setup")
    elif status == "WEAK_CONFIRMATION":
        decision = "APPROVED_REDUCED_SIZE"
        reasons.append("order-flow WEAK_CONFIRMATION -- reduced size")
    elif status == "NEUTRAL":
        if setup.get("confidence", 0) >= float(config["neutral_min_confidence_for_entry"]):
            decision = "APPROVED_REDUCED_SIZE"
            reasons.append("order-flow NEUTRAL but setup confidence clears the entry floor -- reduced size")
        else:
            decision = "WAIT_FOR_CONFIRMATION"
            rejection_reasons.append(f"order-flow NEUTRAL and setup confidence {setup.get('confidence')} below "
                                      f"neutral_min_confidence_for_entry {config['neutral_min_confidence_for_entry']}")
            activation_conditions = _build_activation_conditions(confirmation, setup)
    elif status == "CONTRADICTED":
        decision = "WAIT_FOR_CONFIRMATION"
        rejection_reasons.append("order-flow CONTRADICTED the setup -- never an automatic entry; "
                                  "setup is NOT discarded, watching for order flow to turn")
        activation_conditions = _build_activation_conditions(confirmation, setup)
    elif status == "INSUFFICIENT_DATA":
        dq_score = data_quality.get("data_quality_score", 0)
        if setup.get("price_only_mode") and dq_score >= 50:
            decision = "APPROVED_REDUCED_SIZE"
            reasons.append("order-flow INSUFFICIENT_DATA but setup is price-only and base data quality is adequate -- heavily reduced size")
        else:
            decision = "REJECTED"
            rejection_reasons.append(f"order-flow INSUFFICIENT_DATA and data_quality_score {dq_score} too low to size a position responsibly")
    else:
        decision = "REJECTED"
        rejection_reasons.append(f"unrecognized order-flow status {status}")

    risk_amount = Decimal("0")
    position_size = Decimal("0")
    position_value = Decimal("0")
    portfolio_limits_used: dict = {}

    if decision in ("APPROVED", "APPROVED_REDUCED_SIZE"):
        size_mult = _STATUS_SIZE_MULTIPLIER[status] * _confidence_scale(_d(setup.get("confidence", 0)), config["min_confidence_for_full_size"])
        effective_risk_pct = min(config["base_risk_pct"] * size_mult, config["max_risk_pct"])
        risk_amount = config["paper_capital"] * effective_risk_pct / 100
        position_size = risk_amount / sl_distance
        position_value = position_size * worst_case_entry

        portfolio_check = _check_portfolio_limits(symbol, direction, position_value, risk_amount, portfolio_state, config)
        portfolio_limits_used = portfolio_check["limits_used"]
        if portfolio_check["hard_reject"]:
            decision = "REJECTED"
            rejection_reasons.append(portfolio_check["reason"])
            risk_amount = position_size = position_value = Decimal("0")
        elif portfolio_check["reduced_to_value"] is not None:
            scale = portfolio_check["reduced_to_value"] / position_value if position_value > 0 else Decimal("0")
            position_size *= scale
            position_value *= scale
            risk_amount *= scale
            decision = "APPROVED_REDUCED_SIZE"
            reasons.append(portfolio_check["reason"])

    leverage_estimate = round(position_value / config["paper_capital"], 3) if config["paper_capital"] and position_value else None
    expected_fees = round(position_value * config["taker_fee_rate_pct"] / 100 * 2, 4)
    spread_bps = _d(orderbook.get("spread_bps")) if orderbook.get("available") else None
    estimated_slippage_bps = round(spread_bps * config["slippage_bps_per_unit_spread"], 2) if spread_bps is not None else None

    return {
        "symbol": symbol, "decision": decision, "direction": direction,
        "reference_price": reference_price, "entry_zone": entry_zone, "stop_loss": stop_loss,
        # Sizing/RR are computed off this, NOT reference_price -- see the
        # comment above worst_case_entry's assignment. Reported explicitly
        # so a reader never has to guess which price basis produced the
        # numbers below.
        "worst_case_entry": worst_case_entry,
        "sl_distance_pct": round(sl_distance_pct, 3),
        "risk_amount": round(risk_amount, 2), "position_size": round(position_size, 6),
        "position_value": round(position_value, 2), "leverage_estimate": leverage_estimate,
        "targets": targets, "reward_to_risk": reward_to_risk,
        "expected_fees": expected_fees, "estimated_slippage_bps": estimated_slippage_bps,
        "reasons": reasons, "rejection_reasons": rejection_reasons,
        "activation_conditions": activation_conditions,
        "portfolio_limits_used": portfolio_limits_used, "config_version": config["version"],
    }
