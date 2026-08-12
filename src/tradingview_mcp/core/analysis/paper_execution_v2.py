"""Paper Trading Execution Simulator -- Warstwa 5 (research layer).

Simulates order/position lifecycle against REAL price history -- entry
fills, partial fills, SL/TP exits, partial TP closes, break-even moves,
trailing stops, fees, funding, slippage. NEVER sends a real order to any
exchange; this is a pure, deterministic simulator over already-fetched
1-minute candle windows.

An `APPROVED`/`APPROVED_REDUCED_SIZE` risk decision does NOT mean an
immediate fill -- it means a (simulated) limit order is now live, waiting
for price to actually reach `entry_zone`. Eleven possible states:
SIGNAL_CREATED, PENDING_ENTRY, PARTIALLY_FILLED, OPEN, TP1_HIT, TP2_HIT,
TP3_HIT, STOPPED_OUT, CLOSED, CANCELLED, EXPIRED.
`WAIT_FOR_CONFIRMATION` and `REJECTED` risk decisions NEVER reach this
module at all -- the caller (paper_broker_service_v2) only ever creates
an order for APPROVED/APPROVED_REDUCED_SIZE.

NO LOOK-AHEAD: every function here only ever walks FORWARD through a
`candles_1m` window the caller has already restricted to `<= now` --
nothing here fetches or peeks at future data. If a single 1m candle
contains BOTH the stop loss and a take-profit level (the finest
granularity we have -- no tick data), we cannot know which was touched
first. Per the conservative policy, SL is always assumed to have happened
first in that case (a worse outcome is never silently avoided), the exit
is tagged `ambiguous_intrabar=True`, and the resolution is logged.

Sizing: this module NEVER increases `risk_position_size` -- the cap set
by Warstwa 4 -- it can only fill less (partial fill) or close it down via
exits. Leverage only affects the bookkeeping `margin_used` figure; it
never changes `risk_position_size`, entry/SL/TP prices, or therefore the
maximum possible loss at SL, which is fixed the moment Warstwa 4 sized
the position.

All thresholds (TTL, slippage bps, partial-fill heuristic, TP close
percentages, funding model) are marked HYPOTHESIS -- reasonable starting
points, not calibrated against real Bybit fills or historical funding.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import List, Optional

ORDER_STATES = (
    "SIGNAL_CREATED", "PENDING_ENTRY", "PARTIALLY_FILLED", "OPEN",
    "TP1_HIT", "TP2_HIT", "TP3_HIT", "STOPPED_OUT", "CLOSED", "CANCELLED", "EXPIRED",
)
TERMINAL_STATES = {"STOPPED_OUT", "CLOSED", "CANCELLED", "EXPIRED"}
OPEN_STATES = {"PENDING_ENTRY", "PARTIALLY_FILLED", "OPEN", "TP1_HIT", "TP2_HIT"}

PAPER_CONFIG_VERSION = "1.0"
DEFAULT_PAPER_CONFIG: dict = {
    "version": PAPER_CONFIG_VERSION,
    "initial_capital": Decimal("10000"),
    "entry_ttl_hours": Decimal("12"),
    "entry_slippage_bps": Decimal("2"),
    "exit_slippage_bps": Decimal("2"),
    "taker_fee_rate_pct": Decimal("0.055"),
    "funding_interval_hours": Decimal("8"),
    "funding_rate_default_pct": Decimal("0.01"),
    "tp1_close_pct": Decimal("0.50"),
    "tp2_close_pct": Decimal("0.30"),
    "tp3_close_pct": Decimal("0.20"),
    "move_sl_to_breakeven_on_tp1": True,
    "trailing_stop_enabled": False,
    "trailing_stop_atr_mult": Decimal("1.5"),
    "assumed_leverage": Decimal("3"),
    "ambiguous_intrabar_policy": "ASSUME_SL_FIRST",
    # HYPOTHESIS: candle penetration into entry_zone below this fraction of
    # the zone's width is treated as a partial fill, not a sweep-through
    # full fill -- no real order-book depth simulation exists yet.
    "partial_fill_penetration_threshold": Decimal("0.5"),
    "partial_fill_fraction": Decimal("0.5"),
}


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _log(order: dict, status: str, at: dt.datetime, detail: str) -> None:
    order["status"] = status
    order.setdefault("state_history", []).append({"status": status, "at": at.isoformat(), "detail": detail})


def _touches_zone(candle: dict, low: Decimal, high: Decimal) -> bool:
    c_low, c_high = _d(candle["low"]), _d(candle["high"])
    return c_low <= high and c_high >= low


def _touches_level(candle: dict, level: Decimal, direction: str, side: str) -> bool:
    """side='below' -- level is hit when price trades AT OR BELOW it
    (a LONG's SL/invalidation, or a SHORT's TP). side='above' -- hit when
    price trades AT OR ABOVE it (a LONG's TP, or a SHORT's SL/invalidation)."""
    c_low, c_high = _d(candle["low"]), _d(candle["high"])
    is_below_side = (side == "below")
    return (c_low <= level) if is_below_side else (c_high >= level)


def _sl_side(direction: str) -> str:
    return "below" if direction == "LONG" else "above"


def _tp_side(direction: str) -> str:
    return "above" if direction == "LONG" else "below"


def create_order(symbol: str, signal_id: str, setup: dict, risk_decision: dict, as_of: dt.datetime, config: dict,
                  confirmation_status: str = "UNKNOWN") -> dict:
    """Builds a brand-new order record in SIGNAL_CREATED, immediately
    transitioning to PENDING_ENTRY (a simulated limit order is now live).
    Caller must only call this for APPROVED/APPROVED_REDUCED_SIZE risk
    decisions -- WAIT_FOR_CONFIRMATION/REJECTED never reach here.

    `confirmation_status` (the Warstwa 3 status AT CREATION time) is
    stored so `reevaluate_thesis` can later tell whether order flow has
    genuinely reversed since this order was placed, vs. always having
    been weak."""
    order = {
        "signal_id": signal_id, "symbol": symbol, "direction": setup["direction"],
        "entry_zone": {"low": _d(setup["entry_zone"]["low"]), "high": _d(setup["entry_zone"]["high"])},
        "invalidation": _d(setup.get("invalidation")),
        "original_confirmation_status": confirmation_status,
        "stop_loss": _d(risk_decision["stop_loss"]), "current_sl": _d(risk_decision["stop_loss"]),
        "targets": [_d(t) for t in risk_decision["targets"]],
        "risk_position_size": _d(risk_decision["position_size"]),
        "filled_size": Decimal("0"), "remaining_size": Decimal("0"),
        "avg_entry_price": None, "entry_time": None,
        "created_at": as_of.isoformat(),
        "ttl_expires_at": (as_of + dt.timedelta(hours=float(config["entry_ttl_hours"]))).isoformat(),
        "tp_hit_flags": [False, False, False],
        "exits": [], "sl_moves": [],
        "fees_paid": Decimal("0"), "funding_paid": Decimal("0"), "slippage_cost": Decimal("0"),
        "realized_pnl": Decimal("0"), "mfe": Decimal("0"), "mae": Decimal("0"),
        "last_funding_at": None,
        "config_version": config["version"], "state_history": [],
    }
    _log(order, "SIGNAL_CREATED", as_of, "risk decision approved, order not yet placed")
    _log(order, "PENDING_ENTRY", as_of, "simulated limit order placed, waiting for price to reach entry_zone")
    return order


def reevaluate_thesis(order: dict, fresh_confirmation_status: str, now: dt.datetime) -> dict:
    """Cancels a PENDING_ENTRY/PARTIALLY_FILLED order if order flow has
    flipped to CONTRADICTED since it was created, and wasn't already
    CONTRADICTED then. This is the fix for a gap a live signal pair
    exposed: an order can sit PENDING_ENTRY for however long, but nothing
    previously re-checked whether the order-flow confirmation that
    justified it in the first place still holds -- price-based
    invalidation/TTL alone don't catch a thesis that quietly broke before
    the market ever gave a fill.

    Deliberately narrow trigger (only a flip INTO CONTRADICTED, not any
    softer wobble like CONFIRMED->NEUTRAL) to avoid cancelling on routine
    order-flow noise. Does not touch OPEN/TPx_HIT positions -- once
    filled, SL/TP/trailing-stop rules govern exits, not a fresh
    confirmation re-check (re-litigating a filled position's entire
    thesis every tick would be a different, much noisier design)."""
    if order["status"] not in ("PENDING_ENTRY", "PARTIALLY_FILLED"):
        return order
    original = order.get("original_confirmation_status")
    if original == "CONTRADICTED" or fresh_confirmation_status != "CONTRADICTED":
        return order
    order = dict(order)
    order["state_history"] = list(order.get("state_history", []))
    _log(order, "CANCELLED", now,
         f"order flow reversed before entry: was {original} at creation, now CONTRADICTED -- thesis no longer holds")
    return order


def simulate_entry(order: dict, candles_1m: List[dict], now: dt.datetime, config: dict,
                    regime_changed: bool = False, regime_change_detail: str = "") -> dict:
    """Advances a PENDING_ENTRY/PARTIALLY_FILLED order forward through
    `candles_1m` (already restricted to <= `now` by the caller -- no
    look-ahead happens here). Returns a NEW order dict; never mutates the
    input."""
    order = dict(order)
    order["state_history"] = list(order.get("state_history", []))
    order["entry_zone"] = dict(order["entry_zone"])

    if order["status"] not in ("PENDING_ENTRY", "PARTIALLY_FILLED"):
        return order

    if regime_changed:
        _log(order, "CANCELLED", now, f"regime changed before entry: {regime_change_detail}")
        return order

    direction = order["direction"]
    low, high = order["entry_zone"]["low"], order["entry_zone"]["high"]
    invalidation = order["invalidation"]
    ttl_deadline = dt.datetime.fromisoformat(order["ttl_expires_at"])

    for c in candles_1m:
        ts = c["open_time"]
        if invalidation is not None and _touches_level(c, invalidation, direction, _sl_side(direction)):
            _log(order, "CANCELLED", ts, f"invalidation {invalidation} touched before entry was ever filled")
            return order
        if _touches_zone(c, low, high):
            penetration = _zone_penetration(c, low, high, direction)
            full_fill = penetration >= config["partial_fill_penetration_threshold"]
            fraction = Decimal("1.0") if full_fill else config["partial_fill_fraction"]
            fill_price = _entry_fill_price(c, low, high, direction, config)
            fill_size = order["risk_position_size"] * fraction
            order["filled_size"] = fill_size
            order["remaining_size"] = fill_size
            order["avg_entry_price"] = fill_price
            order["entry_time"] = ts.isoformat()
            entry_notional = fill_size * fill_price
            entry_fee = entry_notional * config["taker_fee_rate_pct"] / 100
            order["fees_paid"] = order["fees_paid"] + entry_fee
            slip = entry_notional * config["entry_slippage_bps"] / 10000
            order["slippage_cost"] = order["slippage_cost"] + slip
            status = "OPEN" if full_fill else "PARTIALLY_FILLED"
            _log(order, status, ts, f"entry filled at {fill_price}, fraction={fraction} "
                                     f"(zone penetration {penetration}, threshold {config['partial_fill_penetration_threshold']})")
            return order
        if ts >= ttl_deadline:
            _log(order, "EXPIRED", ts, f"TTL {config['entry_ttl_hours']}h elapsed without entry_zone being touched")
            return order

    if now >= ttl_deadline:
        _log(order, "EXPIRED", now, f"TTL {config['entry_ttl_hours']}h elapsed without entry_zone being touched (no candle data reached it either)")
    return order


def _zone_penetration(candle: dict, low: Decimal, high: Decimal, direction: str) -> Decimal:
    width = high - low
    if width <= 0:
        return Decimal("1")
    c_low, c_high = _d(candle["low"]), _d(candle["high"])
    overlap_low, overlap_high = max(c_low, low), min(c_high, high)
    overlap = max(Decimal("0"), overlap_high - overlap_low)
    return overlap / width


def _entry_fill_price(candle: dict, low: Decimal, high: Decimal, direction: str, config: dict) -> Decimal:
    """Never assumes the best price in the zone -- uses the zone midpoint
    (a neutral assumption, not the most favorable edge) plus slippage
    working AGAINST the trader, matching real fill behavior better than
    an idealized best-case entry."""
    mid = (low + high) / 2
    slip = mid * config["entry_slippage_bps"] / 10000
    return mid + slip if direction == "LONG" else mid - slip


def simulate_exit(order: dict, candles_1m: List[dict], now: dt.datetime, config: dict) -> dict:
    """Advances an OPEN/PARTIALLY_FILLED/TP1_HIT/TP2_HIT position forward
    through `candles_1m` (already restricted to <= `now`). Handles SL,
    partial TP closes, break-even move (only if
    `move_sl_to_breakeven_on_tp1` is True), trailing stop (only if
    `trailing_stop_enabled` is True), fees, slippage, and funding accrual.
    Returns a NEW order dict; never mutates the input."""
    order = dict(order)
    order["state_history"] = list(order.get("state_history", []))
    order["exits"] = list(order.get("exits", []))
    order["sl_moves"] = list(order.get("sl_moves", []))
    order["tp_hit_flags"] = list(order.get("tp_hit_flags", []))

    if order["status"] not in ("PARTIALLY_FILLED", "OPEN", "TP1_HIT", "TP2_HIT"):
        return order

    direction = order["direction"]
    entry_price = order["avg_entry_price"]
    original_filled = order["filled_size"]
    targets = order["targets"]

    for c in candles_1m:
        ts = c["open_time"]
        if order["remaining_size"] <= 0:
            break

        # MFE/MAE tracked on the remaining position, using the candle's
        # favorable/unfavorable extreme relative to entry.
        favorable = _d(c["high"]) if direction == "LONG" else _d(c["low"])
        unfavorable = _d(c["low"]) if direction == "LONG" else _d(c["high"])
        fav_pct = (favorable - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - favorable) / entry_price * 100
        unfav_pct = (unfavorable - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - unfavorable) / entry_price * 100
        order["mfe"] = max(order["mfe"], fav_pct)
        order["mae"] = min(order["mae"], unfav_pct)

        current_sl = order["current_sl"]
        sl_touched = _touches_level(c, current_sl, direction, _sl_side(direction))
        tp_touched = [not order["tp_hit_flags"][i] and _touches_level(c, targets[i], direction, _tp_side(direction))
                      for i in range(len(targets))]
        any_tp_touched = any(tp_touched)

        if sl_touched and any_tp_touched:
            # AMBIGUOUS_INTRABAR: both an SL and a TP level fall within this
            # single 1m candle's range. No finer data is available here to
            # sequence them -- apply the conservative policy (assume SL
            # first) rather than picking the more favorable outcome.
            _close_remaining(order, current_sl, ts, config, reason="SL",
                              ambiguous=True, resolution_note=f"1m candle contained both SL {current_sl} and a TP level -- "
                                                               f"no finer data available, policy={config['ambiguous_intrabar_policy']}: assumed SL hit first")
            _log(order, "STOPPED_OUT", ts, f"stopped out at {current_sl} (AMBIGUOUS_INTRABAR, resolved conservatively)")
            return order
        if sl_touched:
            _close_remaining(order, current_sl, ts, config, reason="SL", ambiguous=False, resolution_note="")
            _log(order, "STOPPED_OUT", ts, f"stopped out at {current_sl}")
            return order
        if any_tp_touched:
            for i in range(len(targets)):
                if not tp_touched[i]:
                    continue
                close_pct = config[f"tp{i+1}_close_pct"]
                close_size = min(order["remaining_size"], original_filled * close_pct)
                if close_size <= 0:
                    continue
                _partial_close(order, targets[i], close_size, ts, config, level=f"TP{i+1}")
                order["tp_hit_flags"][i] = True
                _log(order, f"TP{i+1}_HIT", ts, f"partial close {close_size} at {targets[i]}")
                if i == 0 and config["move_sl_to_breakeven_on_tp1"] and order["current_sl"] != entry_price:
                    order["sl_moves"].append({"from": str(order["current_sl"]), "to": str(entry_price),
                                               "reason": "move_sl_to_breakeven_on_tp1", "at": ts.isoformat()})
                    order["current_sl"] = entry_price
                if order["remaining_size"] <= 0:
                    _log(order, "CLOSED", ts, "fully exited via TP schedule")
                    return order
            if config["trailing_stop_enabled"] and order["remaining_size"] > 0:
                _apply_trailing_stop(order, c, direction, config, ts)

        order = _accrue_funding_if_due(order, ts, config)

    return order


def _close_remaining(order: dict, price: Decimal, at: dt.datetime, config: dict, reason: str, ambiguous: bool, resolution_note: str) -> None:
    size = order["remaining_size"]
    if size <= 0:
        return
    _partial_close(order, price, size, at, config, level=reason, ambiguous=ambiguous, resolution_note=resolution_note)


def _partial_close(order: dict, price: Decimal, size: Decimal, at: dt.datetime, config: dict, level: str,
                    ambiguous: bool = False, resolution_note: str = "") -> None:
    direction = order["direction"]
    entry_price = order["avg_entry_price"]
    slip = price * config["exit_slippage_bps"] / 10000
    exit_price = price - slip if direction == "LONG" else price + slip
    pnl = (exit_price - entry_price) * size if direction == "LONG" else (entry_price - exit_price) * size
    fee = (size * exit_price) * config["taker_fee_rate_pct"] / 100
    order["fees_paid"] = order["fees_paid"] + fee
    order["slippage_cost"] = order["slippage_cost"] + (size * slip)
    order["realized_pnl"] = order["realized_pnl"] + pnl - fee
    order["remaining_size"] = order["remaining_size"] - size
    order["exits"].append({
        "level": level, "price": str(exit_price), "size": str(size), "at": at.isoformat(),
        "pnl": str(pnl - fee), "ambiguous_intrabar": ambiguous, "resolution_note": resolution_note,
    })


def _apply_trailing_stop(order: dict, candle: dict, direction: str, config: dict, at: dt.datetime) -> None:
    """Only ever fires if explicitly enabled -- never a silent default.
    HYPOTHESIS: trails by `trailing_stop_atr_mult` behind the candle's
    favorable extreme; only ever tightens the stop, never loosens it."""
    favorable = _d(candle["high"]) if direction == "LONG" else _d(candle["low"])
    # ATR isn't threaded into this pure function -- approximate the
    # trailing distance as the original entry-to-SL risk distance, scaled
    # by trailing_stop_atr_mult, rather than inventing an unavailable ATR
    # figure from inside a function that only ever sees OHLC candles.
    original_risk_distance = abs(order["avg_entry_price"] - order["stop_loss"])
    trail_distance = original_risk_distance * config["trailing_stop_atr_mult"]
    new_sl = favorable - trail_distance if direction == "LONG" else favorable + trail_distance
    tightens = (direction == "LONG" and new_sl > order["current_sl"]) or (direction == "SHORT" and new_sl < order["current_sl"])
    if tightens:
        order["sl_moves"].append({"from": str(order["current_sl"]), "to": str(new_sl), "reason": "trailing_stop", "at": at.isoformat()})
        order["current_sl"] = new_sl


def _accrue_funding_if_due(order: dict, now: dt.datetime, config: dict) -> dict:
    """HYPOTHESIS funding model: charges/credits at each
    `funding_interval_hours` boundary since entry, using
    `funding_rate_default_pct` (a real historical funding-rate series is
    not wired into this pure function -- callers with real data should
    pre-compute and pass a more accurate rate if available in a future
    iteration)."""
    if order["remaining_size"] <= 0 or order["entry_time"] is None:
        return order
    entry_time = dt.datetime.fromisoformat(order["entry_time"])
    last_funding = dt.datetime.fromisoformat(order["last_funding_at"]) if order["last_funding_at"] else entry_time
    interval = dt.timedelta(hours=float(config["funding_interval_hours"]))
    next_due = last_funding + interval
    if now < next_due:
        return order
    periods = int((now - last_funding) / interval)
    if periods <= 0:
        return order
    notional = order["remaining_size"] * order["avg_entry_price"]
    rate = config["funding_rate_default_pct"] / 100
    # Longs pay positive funding, shorts receive it (Bybit convention).
    cost_per_period = notional * rate if order["direction"] == "LONG" else -notional * rate
    total_cost = cost_per_period * periods
    order["funding_paid"] = order["funding_paid"] + total_cost
    order["realized_pnl"] = order["realized_pnl"] - total_cost
    order["last_funding_at"] = (last_funding + interval * periods).isoformat()
    return order
