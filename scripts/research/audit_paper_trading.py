"""One-shot audit + validation of paper_execution_v2 / paper_broker_service_v2
("Warstwa 5").

Two phases, recorded into one JSON report:
  1. Synthetic tests (no DB) -- 18 scenarios: LONG/SHORT entry+TP, stop
     loss, a setup that never touches entry (stays PENDING_ENTRY),
     invalidation-before-entry cancellation, TTL expiry, partial fill,
     partial TP (leaves a runner), fees+slippage actually applied,
     funding accrual, SL+TP in the same 1m candle (AMBIGUOUS_INTRABAR,
     resolved conservatively), restart-safety (state round-trips through
     JSON and is not re-created), signal_id deduplication,
     WAIT_FOR_CONFIRMATION never opening a position, REJECTED never
     opening a position, Warstwa 4's position_size never being exceeded,
     a portfolio-exposure check across several persisted positions, and
     deterministic reproduction from identical inputs.
  2. Live run across all tracked symbols -- chains regime -> setup ->
     order-flow confirmation -> risk decision -> paper execution on one
     shared feature snapshot per symbol, using PERSISTENT paper-broker
     state (state/artifacts/research/paper_trading/portfolio_state.json,
     survives a process restart). Fresh signals this run typically stay
     PENDING_ENTRY (nothing is forced) -- that's the correct, expected
     outcome of a first run, not a shortfall. Existing snapshots from
     Warstwa 3/4 have their `outcome` merged in place, never duplicated.

NEVER connects to Bybit's real order-placement endpoint. This is a local
JSON-state simulator only.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

REPORT_DIR = Path("/app/artifacts/research/paper_trading")
REPORT_PATH = REPORT_DIR / "latest_report.json"

report: dict = {
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "synthetic_tests": {},
    "live_run": {},
}

# ── Import check (fatal if this fails -- that IS the point) ────────────────
from tradingview_mcp.core.analysis import paper_execution_v2 as pe  # noqa: E402
from tradingview_mcp.core.analysis import risk_manager_v2 as rm  # noqa: E402
from tradingview_mcp.core.services import paper_broker_service_v2 as pb  # noqa: E402
from tradingview_mcp.core.services import signal_pipeline_v2 as pipeline  # noqa: E402
from tradingview_mcp.core.services import signal_snapshot_service_v2 as snap  # noqa: E402
from tradingview_mcp.core.config.trading_settings import get_trading_settings  # noqa: E402
from tradingview_mcp.core.database.session import session_scope  # noqa: E402

print("IMPORT OK: paper_execution_v2 / paper_broker_service_v2 imported cleanly.", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: synthetic tests (no DB)
# ══════════════════════════════════════════════════════════════════════════

_NOW = dt.datetime(2026, 8, 9, 12, tzinfo=dt.timezone.utc)
_CFG = dict(pe.DEFAULT_PAPER_CONFIG)


def _candle(minutes_from_now, o, h, l, c):
    return {"open_time": _NOW + dt.timedelta(minutes=minutes_from_now), "open": Decimal(str(o)),
            "high": Decimal(str(h)), "low": Decimal(str(l)), "close": Decimal(str(c))}


def _setup(direction="LONG", entry_low=99, entry_high=101, invalidation=95):
    return {"direction": direction, "entry_zone": {"low": entry_low, "high": entry_high}, "invalidation": invalidation}


def _risk_decision(stop_loss=95, targets=(110, 120, 130), position_size=10):
    return {"stop_loss": stop_loss, "targets": list(targets), "position_size": position_size}


def run_synthetic_tests() -> dict:
    results = {}

    def rec(name, ok, expected, got):
        results[name] = {"pass": ok, "expected": expected, "got": got}

    order = pe.create_order("BTCUSDT", "sig1", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 111, 100, 110)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order = pe.simulate_exit(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("1_long_entry_and_tp1", order["status"] == "TP1_HIT" and order["realized_pnl"] > 0,
        "TP1_HIT, positive PnL", f"{order['status']} pnl={order['realized_pnl']}")

    order = pe.create_order("BTCUSDT", "sig2", _setup("SHORT", invalidation=105), _risk_decision(stop_loss=105, targets=(90, 80, 70)), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 100, 89, 90)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order = pe.simulate_exit(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("2_short_entry_and_tp1", order["status"] == "TP1_HIT" and order["realized_pnl"] > 0,
        "TP1_HIT, positive PnL", f"{order['status']} pnl={order['realized_pnl']}")

    order = pe.create_order("BTCUSDT", "sig3", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 100, 94, 95)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order = pe.simulate_exit(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("3_stop_loss_hit", order["status"] == "STOPPED_OUT" and order["realized_pnl"] < 0,
        "STOPPED_OUT, negative PnL", f"{order['status']} pnl={order['realized_pnl']}")

    order = pe.create_order("BTCUSDT", "sig4", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 150, 151, 149, 150)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("4_never_touched_entry_stays_pending", order["status"] == "PENDING_ENTRY", "PENDING_ENTRY", order["status"])

    order = pe.create_order("BTCUSDT", "sig5", _setup("LONG", invalidation=95), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 98, 98, 94, 94)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("5_invalidation_before_entry_cancels", order["status"] == "CANCELLED", "CANCELLED", order["status"])

    order = pe.create_order("BTCUSDT", "sig6", _setup("LONG"), _risk_decision(), _NOW, dict(_CFG, entry_ttl_hours=Decimal("1")))
    order = pe.simulate_entry(order, [], _NOW + dt.timedelta(hours=2), dict(_CFG, entry_ttl_hours=Decimal("1")))
    rec("6_ttl_expiry", order["status"] == "EXPIRED", "EXPIRED", order["status"])

    order = pe.create_order("BTCUSDT", "sig7", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100.9, 101.0, 100.9, 101.0)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("7_partial_fill", order["status"] == "PARTIALLY_FILLED" and order["filled_size"] < Decimal("10"),
        "PARTIALLY_FILLED, filled_size < full size", f"{order['status']} filled={order['filled_size']}")

    order = pe.create_order("BTCUSDT", "sig8", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 111, 100, 110)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order = pe.simulate_exit(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    expected_remaining = Decimal("10") * (Decimal("1") - _CFG["tp1_close_pct"])
    rec("8_partial_tp_leaves_remainder", order["status"] == "TP1_HIT" and order["remaining_size"] == expected_remaining,
        f"TP1_HIT, remaining={expected_remaining}", f"{order['status']} remaining={order['remaining_size']}")

    order = pe.create_order("BTCUSDT", "sig9", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 111, 100, 110)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order = pe.simulate_exit(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("9_fees_and_slippage_applied", order["fees_paid"] > 0 and order["slippage_cost"] > 0,
        "fees_paid > 0 and slippage_cost > 0", f"fees={order['fees_paid']} slippage={order['slippage_cost']}")

    order = pe.create_order("BTCUSDT", "sig10", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    long_window = [_candle(m, 100, 100.5, 99.5, 100) for m in range(10, 10 + 60 * 9, 60)]
    order = pe.simulate_exit(order, long_window, _NOW + dt.timedelta(hours=9), _CFG)
    rec("10_funding_accrues", order["funding_paid"] != 0, "funding_paid != 0", f"funding_paid={order['funding_paid']}")

    order = pe.create_order("BTCUSDT", "sig11", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 115, 90, 105)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order = pe.simulate_exit(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    ambiguous_exit = order["exits"][-1] if order["exits"] else {}
    rec("11_ambiguous_intrabar_conservative_sl_first", order["status"] == "STOPPED_OUT" and ambiguous_exit.get("ambiguous_intrabar") is True,
        "STOPPED_OUT with ambiguous_intrabar=True", f"{order['status']} ambiguous={ambiguous_exit.get('ambiguous_intrabar')}")

    order_a = pe.create_order("BTCUSDT", "sig18", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100), _candle(2, 100, 111, 100, 110)]
    order_a = pe.simulate_entry(order_a, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order_a = pe.simulate_exit(order_a, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order_b = pe.create_order("BTCUSDT", "sig18", _setup("LONG"), _risk_decision(), _NOW, _CFG)
    order_b = pe.simulate_entry(order_b, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    order_b = pe.simulate_exit(order_b, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("18_deterministic_reproduction", order_a == order_b, "identical dicts", f"equal={order_a == order_b}")

    order = pe.create_order("BTCUSDT", "sig16", _setup("LONG"), _risk_decision(position_size=7.5), _NOW, _CFG)
    candles = [_candle(1, 100, 101, 99, 100)]
    order = pe.simulate_entry(order, candles, _NOW + dt.timedelta(minutes=5), _CFG)
    rec("16_never_exceeds_warstwa4_position_size", order["filled_size"] <= Decimal("7.5"), "filled_size <= 7.5", str(order["filled_size"]))

    return results


async def run_async_synthetic_tests() -> dict:
    """Service-layer scenarios that need process_signal's dedup logic --
    none of these actually touch the DB (new-order-creation and terminal-
    order paths both short-circuit before any query)."""
    results = {}
    setup = {"direction": "LONG", "entry_zone": {"low": 99, "high": 101}, "invalidation": 95}
    approved = {"decision": "APPROVED", "stop_loss": 95, "targets": [110, 120, 130], "position_size": 10}
    wait = {"decision": "WAIT_FOR_CONFIRMATION", "stop_loss": 95, "targets": [110, 120, 130], "position_size": 10}
    rejected = {"decision": "REJECTED", "stop_loss": 95, "targets": [110, 120, 130], "position_size": 10}

    state = pb.fresh_state(_CFG)
    r = await pb.process_signal(None, "ETHUSDT", "sigA", setup, wait, "CONFIRMED", _NOW, state, _CFG)
    results["14_wait_for_confirmation_never_opens"] = {"pass": r is None and "sigA" not in state["orders"],
                                                        "expected": "no order created", "got": f"order={r} orders={list(state['orders'])}"}

    state = pb.fresh_state(_CFG)
    r = await pb.process_signal(None, "ETHUSDT", "sigB", setup, rejected, "CONFIRMED", _NOW, state, _CFG)
    results["15_rejected_never_opens"] = {"pass": r is None and "sigB" not in state["orders"],
                                           "expected": "no order created", "got": f"order={r} orders={list(state['orders'])}"}

    state = pb.fresh_state(_CFG)
    await pb.process_signal(None, "ETHUSDT", "sigC", setup, approved, "WEAK_CONFIRMATION", _NOW, state, _CFG)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        pb.save_state(state, path=path)
        state2 = pb.load_state(_CFG, path=path)
        results["12_restart_safety_state_preserved"] = {
            "pass": state2["orders"]["sigC"]["status"] == "PENDING_ENTRY" and state2["orders"]["sigC"]["risk_position_size"] == Decimal("10"),
            "expected": "state survives JSON round-trip with correct types", "got": str(state2["orders"]["sigC"]["status"]),
        }
        r2 = await pb.process_signal(None, "ETHUSDT", "sigC", setup, approved, "WEAK_CONFIRMATION", _NOW, state2, _CFG)
        results["13_signal_id_deduplication"] = {
            "pass": len(state2["orders"]) == 1 and r2["signal_id"] == "sigC",
            "expected": "no duplicate order for the same signal_id", "got": f"orders_count={len(state2['orders'])}",
        }
        # Same order, but THIS tick's order flow has reversed to
        # CONTRADICTED -- must cancel the still-pending order rather than
        # leave it sitting on a thesis that no longer holds (the exact gap
        # a live LTCUSDT/SUIUSDT signal pair exposed: CVD/confirmation
        # flipped within minutes of creation while the order sat untouched).
        r3 = await pb.process_signal(None, "ETHUSDT", "sigC", setup, approved, "CONTRADICTED", _NOW + dt.timedelta(minutes=12), state2, _CFG)
        results["19_pending_order_cancelled_on_thesis_reversal"] = {
            "pass": r3["status"] == "CANCELLED" and "order flow reversed" in r3["state_history"][-1]["detail"],
            "expected": "CANCELLED once fresh confirmation flips to CONTRADICTED", "got": r3["status"],
        }

    state = pb.fresh_state(_CFG)
    o1 = pe.create_order("SOLUSDT", "sigD", setup, approved, _NOW, _CFG)
    o1["status"] = "OPEN"; o1["remaining_size"] = Decimal("10"); o1["avg_entry_price"] = Decimal("100")
    o2 = pe.create_order("BNBUSDT", "sigE", setup, approved, _NOW, _CFG)
    o2["status"] = "OPEN"; o2["remaining_size"] = Decimal("5"); o2["avg_entry_price"] = Decimal("200")
    state["orders"] = {"sigD": o1, "sigE": o2}
    pf = pb.build_portfolio_state_for_risk(state)
    total = sum(p["position_value"] for p in pf["open_positions"])
    results["17_portfolio_exposure_summed_across_positions"] = {
        "pass": len(pf["open_positions"]) == 2 and total == Decimal("2000"),
        "expected": "2 positions, total exposure 2000", "got": f"count={len(pf['open_positions'])} total={total}",
    }
    return results


try:
    report["synthetic_tests"] = run_synthetic_tests()
    report["synthetic_tests"].update(asyncio.run(run_async_synthetic_tests()))
except Exception:
    report["synthetic_tests"] = {"_error": traceback.format_exc()}

synth_failed = [k for k, v in report["synthetic_tests"].items() if isinstance(v, dict) and not v.get("pass", False)]


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: live run -- regime -> setup -> confirmation -> risk -> paper
# execution, on PERSISTENT paper-broker state.
# ══════════════════════════════════════════════════════════════════════════

async def run_live_pipeline() -> dict:
    settings = get_trading_settings()
    symbols = list(settings.symbols)
    ordered = (["BTCUSDT"] if "BTCUSDT" in symbols else []) + [s for s in symbols if s != "BTCUSDT"]

    paper_config = pe.DEFAULT_PAPER_CONFIG
    risk_config = rm.DEFAULT_RISK_CONFIG
    now = dt.datetime.now(dt.timezone.utc)

    state = pb.load_state(paper_config)
    state = pb.apply_daily_weekly_rollover(state, now)
    state.setdefault("last_seen_regime", {})

    results = {}
    all_snapshots = []
    outcome_updates = {}
    current_prices = {}
    btc_regime = None

    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await pipeline.run_pipeline_for_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                if sym == "BTCUSDT":
                    btc_regime = r["regime"]["primary_regime"]
                current_prices[sym] = r["price_at_decision"]

                last_regime = state["last_seen_regime"].get(sym)
                regime_changed = last_regime is not None and last_regime != r["regime"]["primary_regime"]
                state["last_seen_regime"][sym] = r["regime"]["primary_regime"]

                risk_decisions = {}
                paper_orders = {}
                for setup_type, s in r["setups"].items():
                    confirmation = r["confirmations"][setup_type]
                    portfolio_state_for_risk = pb.build_portfolio_state_for_risk(state)
                    decision = rm.evaluate_risk(sym, r["as_of"], r["regime"], s, confirmation, r["orderbook"],
                                                 r["data_quality"], portfolio_state_for_risk, risk_config)
                    risk_decisions[setup_type] = decision

                    if s["direction"] not in ("LONG", "SHORT"):
                        continue
                    regime_ts = (r["data_quality"].get("source_timestamps") or {}).get("candles")
                    signal_id = snap.make_signal_id(sym, setup_type, s["direction"], r["as_of"], regime_ts)
                    order = await pb.process_signal(session, sym, signal_id, s, decision, confirmation["status"],
                                                      r["as_of"], state, paper_config,
                                                      regime_changed=regime_changed,
                                                      regime_change_detail=f"{last_regime} -> {r['regime']['primary_regime']}" if regime_changed else "")
                    if order is not None:
                        paper_orders[setup_type] = order
                        outcome_updates[signal_id] = pb.order_to_outcome_update(order, decision.get("risk_amount"))

                r["risk_decisions"] = risk_decisions
                r["paper_orders"] = paper_orders
                results[sym] = r

                records = snap.build_snapshots(sym, r["as_of"], r["regime"], r["setups"], r["confirmations"],
                                                risk_decisions, r["data_quality"], r["price_at_decision"])
                all_snapshots.extend(records)
            except Exception:
                results[sym] = {"error": traceback.format_exc()}

    try:
        write_result = snap.append_snapshots(all_snapshots)
        update_result = snap.update_snapshot_outcomes(outcome_updates)
        snapshot_error = None
    except Exception:
        write_result, update_result = {"written": 0, "duplicates_skipped": 0}, {"updated": 0, "not_found": []}
        snapshot_error = traceback.format_exc()

    account = pb.compute_account_summary(state, current_prices, paper_config)
    try:
        pb.save_state(state)
        state_save_error = None
    except Exception:
        state_save_error = traceback.format_exc()

    valid = {s: r for s, r in results.items() if "error" not in r}
    chain_summary = {}
    for sym, r in valid.items():
        chain = {}
        for setup_type, s in r["setups"].items():
            if s["direction"] == "NO_SETUP":
                continue
            conf = r["confirmations"][setup_type]
            risk = r["risk_decisions"][setup_type]
            order = r["paper_orders"].get(setup_type)
            chain[setup_type] = {
                "direction": s["direction"], "confirmation": conf["status"], "risk_decision": risk["decision"],
                "paper_status": order["status"] if order else "(no order -- WAIT/REJECTED)",
                "filled_size": str(order["filled_size"]) if order else None,
            }
        chain_summary[sym] = {"regime": r["regime"]["primary_regime"], "chain": chain}

    return {
        "chain_summary": chain_summary,
        "account_summary": account,
        "snapshots_written": write_result["written"], "snapshots_duplicates_skipped": write_result["duplicates_skipped"],
        "outcomes_updated": update_result["updated"], "outcomes_not_found": update_result["not_found"],
        "snapshot_error": snapshot_error, "state_save_error": state_save_error,
        "state_path": str(pb.STATE_PATH), "snapshot_path": str(snap.SNAPSHOT_PATH),
    }


try:
    report["live_run"] = asyncio.run(run_live_pipeline())
except Exception:
    report["live_run"] = {"error": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════════
# Status
# ══════════════════════════════════════════════════════════════════════════

n_pass = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict) and v.get("pass"))
n_total = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict))

report["status"] = "IMPLEMENTATION_COMPLETE_VALIDATION_PENDING"
report["layer_5_status"] = {
    "logically_architecturally_complete": True,
    "synthetically_tested": n_pass == n_total and n_total > 0,
    "synthetic_tests_pass": f"{n_pass}/{n_total}",
    "live_smoke_tested": "error" not in report.get("live_run", {"error": True}),
    "statistically_validated": False,
    "statistically_validated_reason": "first live run -- no positions have had time to reach a terminal state yet",
    "production_ready": False,
    "connects_to_real_exchange_order_endpoint": False,
    "note": "paper_execution_v2 NEVER sends a real order -- local JSON-state simulator only, coexists with (does not touch) the existing production paper-broker DB tables/container.",
}

REPORT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

print("=" * 70)
print("PAPER TRADING v2 -- AUDIT REPORT (Warstwa 5)")
print(f"STATUS: {report['status']}")
print("=" * 70)
print(f"Synthetic tests: {n_pass}/{n_total} PASS")
if synth_failed:
    for name in synth_failed:
        t = report["synthetic_tests"][name]
        print(f"  FAILED: {name} -> expected {t['expected']}, got {t['got']}")
print("-" * 70)
print("Live run -- regime -> setup -> order-flow -> risk -> paper execution chain:")
lr = report["live_run"]
if "error" in lr:
    print(f"  ERROR: {lr['error'][-2000:]}")
else:
    for sym, c in lr["chain_summary"].items():
        if not c["chain"]:
            print(f"  {sym:10s} regime={c['regime']:22s} (no setup fired)")
            continue
        for setup_type, entry in c["chain"].items():
            print(f"  {sym:10s} regime={c['regime']:22s} {setup_type}:{entry['direction']} "
                  f"conf.flow={entry['confirmation']:16s} risk={entry['risk_decision']:22s} "
                  f"paper={entry['paper_status']} filled={entry['filled_size']}")
    acc = lr["account_summary"]
    print(f"  account: initial_capital={acc['initial_capital']} balance={acc['balance']} "
          f"realized_pnl={acc['realized_pnl']} unrealized_pnl={acc['unrealized_pnl']} equity={acc['equity']}")
    print(f"  margin_used={acc['margin_used']} margin_available={acc['margin_available']}")
    print(f"  daily_realized_pnl={acc['daily_realized_pnl']} weekly_realized_pnl={acc['weekly_realized_pnl']} "
          f"max_drawdown_pct={acc['max_drawdown_pct']}")
    print(f"  active_orders (PENDING_ENTRY): {len(acc['active_orders'])}")
    for o in acc["active_orders"]:
        print(f"    {o['symbol']:10s} {o['direction']:5s} entry_zone={o['entry_zone']}")
    print(f"  open_positions: {len(acc['open_positions'])}")
    for o in acc["open_positions"]:
        print(f"    {o['symbol']:10s} {o['direction']:5s} status={o['status']} remaining={o['remaining_size']} "
              f"entry={o['avg_entry_price']} sl={o['current_sl']} upl={o['unrealized_pnl']}")
    print(f"  snapshots written: {lr['snapshots_written']} (dup skipped: {lr['snapshots_duplicates_skipped']}) -> {lr['snapshot_path']}")
    print(f"  outcomes updated (existing snapshots): {lr['outcomes_updated']} (not found: {lr['outcomes_not_found']})")
    print(f"  paper broker state persisted -> {lr['state_path']}")
    if lr.get("snapshot_error"):
        print(f"  SNAPSHOT ERROR: {lr['snapshot_error'][-1000:]}")
    if lr.get("state_save_error"):
        print(f"  STATE SAVE ERROR: {lr['state_save_error'][-1000:]}")
print("-" * 70)
print(f"Full report written to: {REPORT_PATH}")
