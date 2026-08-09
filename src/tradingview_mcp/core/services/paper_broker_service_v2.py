"""Persistent paper-trading broker service -- Warstwa 5 assembly layer.

Wraps `paper_execution_v2` (the pure simulator) with: durable JSON state
(survives a process restart), dedup keyed on `signal_id` (an order is
created AT MOST ONCE per signal, ever), and DB reads of already-closed 1m
candles to advance existing orders forward in time.

Coexists with (does NOT touch) the existing production paper-broker
tables (`core/database/models/paper_trading.py` -- `PaperPosition`,
`PaperEquitySnapshot`, `DailyRiskState`, used by the `trading-paper-broker`
container already running in this stack). This is additive research-track
code with its own JSON-file state, same convention as every other _v2
module in this project.

NEVER sends a real order to any exchange -- this only reads already-
persisted candle history and updates a local JSON ledger.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.analysis import paper_execution_v2 as pe
from tradingview_mcp.core.database.repositories import query_repository as qr

STATE_PATH = Path("/app/artifacts/research/paper_trading/portfolio_state.json")

_ORDER_DECIMAL_FIELDS = (
    "risk_position_size", "filled_size", "remaining_size", "avg_entry_price",
    "stop_loss", "current_sl", "invalidation", "fees_paid", "funding_paid",
    "slippage_cost", "realized_pnl", "mfe", "mae",
)
_STATE_DECIMAL_FIELDS = ("initial_capital", "balance", "peak_equity", "max_drawdown_pct",
                          "daily_realized_pnl", "weekly_realized_pnl")


def _decimalize_order(order: dict) -> dict:
    o = dict(order)
    for f in _ORDER_DECIMAL_FIELDS:
        if o.get(f) is not None:
            o[f] = Decimal(str(o[f]))
    if o.get("entry_zone"):
        o["entry_zone"] = {"low": Decimal(str(o["entry_zone"]["low"])), "high": Decimal(str(o["entry_zone"]["high"]))}
    if o.get("targets"):
        o["targets"] = [Decimal(str(t)) for t in o["targets"]]
    return o


def _candle_dict(c) -> dict:
    return {"open_time": c.open_time, "open": c.open, "high": c.high, "low": c.low, "close": c.close}


def fresh_state(config: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "config_version": config["version"],
        "initial_capital": config["initial_capital"],
        "balance": config["initial_capital"],
        "orders": {},
        "peak_equity": config["initial_capital"],
        "max_drawdown_pct": Decimal("0"),
        "daily_period_start": now.date().isoformat(),
        "daily_realized_pnl": Decimal("0"),
        "weekly_period_start": now.date().isoformat(),
        "weekly_realized_pnl": Decimal("0"),
        "created_at": now.isoformat(),
        "last_updated": now.isoformat(),
    }


def load_state(config: dict, path: Optional[Path] = None) -> dict:
    p = path or STATE_PATH
    if not p.exists():
        return fresh_state(config)
    raw = json.loads(p.read_text())
    state = dict(raw)
    for f in _STATE_DECIMAL_FIELDS:
        state[f] = Decimal(str(state[f]))
    state["orders"] = {sid: _decimalize_order(o) for sid, o in raw.get("orders", {}).items()}
    return state


def save_state(state: dict, path: Optional[Path] = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(p)  # atomic rename on POSIX -- no half-written state file on crash


def _last_progress_ts(order: dict) -> dt.datetime:
    history = order.get("state_history") or []
    if history:
        return dt.datetime.fromisoformat(history[-1]["at"])
    return dt.datetime.fromisoformat(order["created_at"])


def apply_daily_weekly_rollover(state: dict, now: dt.datetime) -> dict:
    state = dict(state)
    today = now.date()
    if today.isoformat() != state["daily_period_start"]:
        state["daily_period_start"] = today.isoformat()
        state["daily_realized_pnl"] = Decimal("0")
    week_start = (today - dt.timedelta(days=today.weekday())).isoformat()
    if week_start != state.get("weekly_period_start"):
        state["weekly_period_start"] = week_start
        state["weekly_realized_pnl"] = Decimal("0")
    return state


async def process_signal(
    session: AsyncSession, symbol: str, signal_id: str, setup: dict, risk_decision: dict,
    as_of: dt.datetime, state: dict, config: dict,
    regime_changed: bool = False, regime_change_detail: str = "",
) -> Optional[dict]:
    """Creates a new order AT MOST ONCE per `signal_id` (dedup), and only
    ever for APPROVED/APPROVED_REDUCED_SIZE -- WAIT_FOR_CONFIRMATION and
    REJECTED NEVER reach `paper_execution_v2.create_order`. For an
    existing order, advances it forward using only 1m candles strictly
    after its last-processed checkpoint (no look-ahead, no re-processing
    already-seen candles on a re-run)."""
    orders = state["orders"]
    if signal_id not in orders:
        if risk_decision.get("decision") not in ("APPROVED", "APPROVED_REDUCED_SIZE"):
            return None
        order = pe.create_order(symbol, signal_id, setup, risk_decision, as_of, config)
        orders[signal_id] = order
        _settle_costs_into_balance(state, order, delta_only=True, previous=None)
        return order

    order = orders[signal_id]
    previous = dict(order)
    if order["status"] in pe.TERMINAL_STATES:
        return order

    last_ts = _last_progress_ts(order)
    if last_ts >= as_of:
        return order

    rows = await qr.get_candles_between(session, symbol, "1", last_ts, as_of, limit=5000)
    candles = [_candle_dict(c) for c in rows]

    if order["status"] in ("PENDING_ENTRY", "PARTIALLY_FILLED"):
        order = pe.simulate_entry(order, candles, as_of, config, regime_changed=regime_changed,
                                   regime_change_detail=regime_change_detail)
    if order["status"] in ("PARTIALLY_FILLED", "OPEN", "TP1_HIT", "TP2_HIT"):
        order = pe.simulate_exit(order, candles, as_of, config)

    orders[signal_id] = order
    _settle_costs_into_balance(state, order, delta_only=True, previous=previous)
    return order


def _settle_costs_into_balance(state: dict, order: dict, delta_only: bool, previous: Optional[dict]) -> None:
    """Folds newly-realized PnL/fees/funding into `balance` and the daily/
    weekly realized-PnL trackers -- only the DELTA since the previous
    snapshot of this order, so re-processing never double-counts."""
    prev_realized = previous["realized_pnl"] if previous else Decimal("0")
    delta = order["realized_pnl"] - prev_realized
    if delta == 0:
        return
    state["balance"] = state["balance"] + delta
    state["daily_realized_pnl"] = state["daily_realized_pnl"] + delta
    state["weekly_realized_pnl"] = state["weekly_realized_pnl"] + delta


def position_notional_for_exposure(order: dict) -> Decimal:
    """Conservative exposure figure: a still-pending order's full
    risk_position_size at the entry-zone midpoint counts as committed
    exposure (nothing is filled yet, but the intent is), an open/partial
    position uses its actual remaining filled size at entry price."""
    if order["status"] in ("PENDING_ENTRY",):
        mid = (order["entry_zone"]["low"] + order["entry_zone"]["high"]) / 2
        return order["risk_position_size"] * mid
    if order["status"] in ("PARTIALLY_FILLED", "OPEN", "TP1_HIT", "TP2_HIT"):
        return order["remaining_size"] * (order["avg_entry_price"] or Decimal("0"))
    return Decimal("0")


def build_portfolio_state_for_risk(state: dict) -> dict:
    """Derives the `open_positions` list risk_manager_v2.evaluate_risk
    expects directly from the PERSISTED paper-broker state -- Warstwa 4
    now respects real carried-over exposure across restarts, not just
    positions opened earlier in the same run."""
    open_positions = []
    for order in state["orders"].values():
        if order["status"] in pe.OPEN_STATES:
            open_positions.append({"symbol": order["symbol"], "direction": order["direction"],
                                    "position_value": position_notional_for_exposure(order)})
    capital = state["initial_capital"]
    daily_pct = (state["daily_realized_pnl"] / capital * 100) if capital else Decimal("0")
    weekly_pct = (state["weekly_realized_pnl"] / capital * 100) if capital else Decimal("0")
    return {"open_positions": open_positions, "daily_realized_pnl_pct": daily_pct,
            "weekly_realized_pnl_pct": weekly_pct, "btc_regime_flip_detected": False}


def compute_account_summary(state: dict, current_prices: dict, config: dict) -> dict:
    """`current_prices` maps symbol -> latest known price, used only for
    UNREALIZED pnl / margin reporting -- never affects any stored decision."""
    unrealized = Decimal("0")
    margin_used = Decimal("0")
    open_positions_report = []
    active_orders_report = []
    leverage = config["assumed_leverage"]
    for order in state["orders"].values():
        if order["status"] in ("PENDING_ENTRY",):
            active_orders_report.append({"signal_id": order["signal_id"], "symbol": order["symbol"],
                                          "direction": order["direction"], "status": order["status"],
                                          "entry_zone": order["entry_zone"]})
        elif order["status"] in ("PARTIALLY_FILLED", "OPEN", "TP1_HIT", "TP2_HIT"):
            price = current_prices.get(order["symbol"], order["avg_entry_price"])
            notional = order["remaining_size"] * (order["avg_entry_price"] or Decimal("0"))
            margin_used += notional / leverage if leverage else notional
            upl = Decimal("0")
            if price is not None and order["avg_entry_price"] is not None:
                upl = ((price - order["avg_entry_price"]) * order["remaining_size"] if order["direction"] == "LONG"
                       else (order["avg_entry_price"] - price) * order["remaining_size"])
                unrealized += upl
            open_positions_report.append({"signal_id": order["signal_id"], "symbol": order["symbol"],
                                           "direction": order["direction"], "status": order["status"],
                                           "remaining_size": order["remaining_size"], "avg_entry_price": order["avg_entry_price"],
                                           "current_sl": order["current_sl"], "unrealized_pnl": upl})

    equity = state["balance"] + unrealized
    peak_equity = max(state["peak_equity"], equity)
    drawdown_pct = ((peak_equity - equity) / peak_equity * 100) if peak_equity else Decimal("0")
    max_drawdown_pct = max(state["max_drawdown_pct"], drawdown_pct)
    # Persist the drawdown watermark back into state -- without this, peak
    # equity and max drawdown would never actually survive a restart, only
    # ever computed freshly (and wrongly reset) each call.
    state["peak_equity"] = peak_equity
    state["max_drawdown_pct"] = max_drawdown_pct

    return {
        "initial_capital": state["initial_capital"], "balance": state["balance"],
        "realized_pnl": state["balance"] - state["initial_capital"], "unrealized_pnl": unrealized,
        "equity": equity, "margin_used": margin_used, "margin_available": max(Decimal("0"), equity - margin_used),
        "open_positions": open_positions_report, "active_orders": active_orders_report,
        "daily_realized_pnl": state["daily_realized_pnl"], "weekly_realized_pnl": state["weekly_realized_pnl"],
        "peak_equity": peak_equity, "current_drawdown_pct": drawdown_pct, "max_drawdown_pct": max_drawdown_pct,
    }


def order_to_outcome_update(order: dict, risk_amount: Optional[Decimal]) -> dict:
    """Converts a paper_execution_v2 order record into the partial
    `outcome` dict signal_snapshot_service_v2.update_snapshot_outcomes
    merges into the ORIGINAL Warstwa 1-4 snapshot. `price_after_15m/1h/
    4h/12h` are intentionally left absent here -- a fixed-horizon price
    sample is a separate, larger feature (a dedicated future evaluator
    pass), not something this incremental live-run updater computes."""
    exits = order.get("exits") or []
    tp_exits = {e["level"]: e for e in exits if e["level"] in ("TP1", "TP2", "TP3")}
    sl_exit = next((e for e in exits if e["level"] == "SL"), None)
    is_terminal = order["status"] in pe.TERMINAL_STATES

    duration_hours = None
    if order.get("entry_time") and exits:
        entry_dt = dt.datetime.fromisoformat(order["entry_time"])
        last_exit_dt = dt.datetime.fromisoformat(exits[-1]["at"])
        duration_hours = round((last_exit_dt - entry_dt).total_seconds() / 3600, 4)

    r_multiple = None
    if risk_amount and risk_amount != 0:
        r_multiple = float(order["realized_pnl"] / risk_amount)

    sl_or_tp_first = None
    if exits:
        first = exits[0]
        sl_or_tp_first = "SL" if first["level"] == "SL" else "TP"

    return {
        "entry_touched": order["status"] not in ("PENDING_ENTRY",),
        "entry_time": order.get("entry_time"),
        "actual_entry_price": order.get("avg_entry_price"),
        "filled_size": order.get("filled_size"),
        "tp1_time": (tp_exits.get("TP1") or {}).get("at"), "tp1_price": (tp_exits.get("TP1") or {}).get("price"),
        "tp2_time": (tp_exits.get("TP2") or {}).get("at"), "tp2_price": (tp_exits.get("TP2") or {}).get("price"),
        "tp3_time": (tp_exits.get("TP3") or {}).get("at"), "tp3_price": (tp_exits.get("TP3") or {}).get("price"),
        "sl_time": sl_exit["at"] if sl_exit else None, "sl_price": sl_exit["price"] if sl_exit else None,
        "sl_or_tp_first": sl_or_tp_first,
        "fees_paid": order.get("fees_paid"), "funding_paid": order.get("funding_paid"), "slippage_cost": order.get("slippage_cost"),
        "realized_pnl": order.get("realized_pnl"), "r_multiple": r_multiple,
        "mfe": order.get("mfe"), "mae": order.get("mae"),
        "duration_hours": duration_hours, "final_status": order["status"],
        "evaluated": is_terminal, "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat() if is_terminal else None,
    }
