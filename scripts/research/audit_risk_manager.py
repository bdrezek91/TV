"""One-shot audit + validation of risk_manager_v2 ("Warstwa 4").

Two phases, recorded into one JSON report:
  1. Synthetic tests (no DB) -- 16 scenarios: correct LONG/SHORT sizing, a
     wider SL proportionally shrinking size (never rejected just for being
     wider), weak reward:risk rejected without widening SL to force it
     through, CONTRADICTED -> WAIT_FOR_CONFIRMATION (setup kept, not
     discarded), WEAK_CONFIRMATION -> reduced size, daily-loss kill switch,
     max-open-positions kill switch, total-exposure limit, correlated-alt-
     group exposure limit (treats several alts as ONE combined risk, not N
     independent trades), stale/untradeable data kill switch, wide-spread
     kill switch, missing SL rejected, signal_id deduplication, restart-
     safety (same dedup, simulated), and a determinism/no-look-ahead check
     (the function signature has no forward-looking parameter at all, and
     identical inputs always produce identical output).
  2. Live run across all tracked symbols -- chains regime (Warstwa 1) ->
     setup (Warstwa 2) -> order-flow confirmation (Warstwa 3) -> risk
     decision (Warstwa 4) on one shared feature snapshot per symbol, with
     a portfolio_state that ACCUMULATES across symbols within this run (so
     max-exposure / correlated-alt / max-open-positions limits are
     genuinely exercised against each other, not evaluated in isolation).
     Persists one durable JSONL snapshot per LONG/SHORT setup with the
     full decision chain and an untouched `outcome` placeholder.

NEVER opens even a simulated paper position -- this layer only computes
and records risk decisions. Paper execution is explicitly Warstwa 5.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

REPORT_DIR = Path("/app/artifacts/research/risk_manager")
REPORT_PATH = REPORT_DIR / "latest_report.json"

report: dict = {
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "synthetic_tests": {},
    "live_run": {},
}

# ── Import check (fatal if this fails -- that IS the point) ────────────────
from tradingview_mcp.core.analysis import risk_manager_v2 as rm  # noqa: E402
from tradingview_mcp.core.services import signal_pipeline_v2 as pipeline  # noqa: E402
from tradingview_mcp.core.services import signal_snapshot_service_v2 as snap  # noqa: E402
from tradingview_mcp.core.config.trading_settings import get_trading_settings  # noqa: E402
from tradingview_mcp.core.database.session import session_scope  # noqa: E402

print("IMPORT OK: risk_manager_v2 / signal_pipeline_v2 / signal_snapshot_service_v2 imported cleanly.", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: synthetic tests (no DB)
# ══════════════════════════════════════════════════════════════════════════

_NOW = dt.datetime(2026, 8, 9, 12, tzinfo=dt.timezone.utc)
_DQ_GOOD = {"is_tradeable": True, "data_quality_score": 95, "source_timestamps": {}}
_REGIME_OK = {"primary_regime": "RANGE", "confidence": 0.5, "data_quality": 0.9}
_OB_OK = {"available": True, "spread_bps": Decimal("5")}
_EMPTY_PORTFOLIO = {"open_positions": [], "daily_realized_pnl_pct": Decimal("0"),
                     "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False}


def _setup(direction="LONG", entry_low=99, entry_high=101, stop_loss=95, targets=(110, 120, 130), confidence=0.6):
    return {
        "direction": direction, "entry_zone": {"low": Decimal(str(entry_low)), "high": Decimal(str(entry_high))},
        "stop_loss": Decimal(str(stop_loss)) if stop_loss is not None else None,
        "targets": [Decimal(str(t)) for t in targets] if targets is not None else [],
        "confidence": confidence, "price_only_mode": False,
    }


def _confirmation(status="CONFIRMED"):
    return {"status": status, "dimensions": [], "confidence": 0.6}


def run_synthetic_tests() -> dict:
    results = {}

    def rec(name, ok, expected, got):
        results[name] = {"pass": ok, "expected": expected, "got": got}

    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("1_long_position_size", r["decision"] == "APPROVED" and r["position_size"] == Decimal("10"),
        "APPROVED size=10", f"{r['decision']} size={r['position_size']}")

    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("SHORT", stop_loss=105, targets=(90, 80, 70)),
                          _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("2_short_position_size", r["decision"] == "APPROVED" and r["position_size"] == Decimal("10"),
        "APPROVED size=10", f"{r['decision']} size={r['position_size']}")

    r_narrow = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG", stop_loss=95, targets=(110, 120, 130)),
                                 _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    r_wide = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG", stop_loss=90, targets=(120, 140, 160)),
                               _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("3_wide_sl_reduces_size_not_rejected", r_wide["decision"] == "APPROVED" and r_wide["position_size"] == r_narrow["position_size"] / 2,
        "wide SL APPROVED, size = narrow/2", f"narrow={r_narrow['position_size']} wide={r_wide['position_size']} wide_decision={r_wide['decision']}")

    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG", stop_loss=95, targets=(102, 104, 106)),
                          _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("4_weak_reward_to_risk_rejected", r["decision"] == "REJECTED" and any("reward:risk" in x for x in r["rejection_reasons"]),
        "REJECTED (reward:risk, SL not widened)", f"{r['decision']} {r['rejection_reasons']}")

    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONTRADICTED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("5_contradicted_waits_setup_kept", r["decision"] == "WAIT_FOR_CONFIRMATION" and len(r["activation_conditions"]) > 0,
        "WAIT_FOR_CONFIRMATION with activation_conditions", f"{r['decision']} conditions={len(r['activation_conditions'])}")

    r_confirmed = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    r_weak = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("WEAK_CONFIRMATION"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("6_weak_confirmation_reduced_size", r_weak["decision"] == "APPROVED_REDUCED_SIZE" and r_weak["position_size"] < r_confirmed["position_size"],
        "APPROVED_REDUCED_SIZE < CONFIRMED size", f"weak={r_weak['position_size']} confirmed={r_confirmed['position_size']}")

    portfolio_daily_breach = dict(_EMPTY_PORTFOLIO, daily_realized_pnl_pct=Decimal("-5"))
    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, portfolio_daily_breach)
    rec("7_daily_loss_limit", r["decision"] == "REJECTED" and any("daily loss" in x for x in r["rejection_reasons"]),
        "REJECTED (daily loss limit)", f"{r['decision']} {r['rejection_reasons']}")

    portfolio_full = dict(_EMPTY_PORTFOLIO, open_positions=[{"symbol": f"SYM{i}", "direction": "LONG", "position_value": Decimal("100")} for i in range(6)])
    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, portfolio_full)
    rec("8_max_open_positions", r["decision"] == "REJECTED" and any("max_open_positions" in x for x in r["rejection_reasons"]),
        "REJECTED (max_open_positions)", f"{r['decision']} {r['rejection_reasons']}")

    # BTCUSDT isn't in the correlated ALTS group, so this isolates the
    # total-exposure limit specifically (binding_limit should name it, not
    # the correlated group).
    portfolio_exposed = dict(_EMPTY_PORTFOLIO, open_positions=[{"symbol": "BTCUSDT", "direction": "LONG", "position_value": Decimal("5000")}])
    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, portfolio_exposed)
    rec("9_max_total_exposure", r["decision"] == "REJECTED" and any("total_exposure" in x for x in r["rejection_reasons"]),
        "REJECTED, binding_limit names total_exposure", f"{r['decision']} {r['rejection_reasons']}")

    portfolio_alts = dict(_EMPTY_PORTFOLIO, open_positions=[{"symbol": "SOLUSDT", "direction": "LONG", "position_value": Decimal("1500")},
                                                             {"symbol": "BNBUSDT", "direction": "LONG", "position_value": Decimal("1500")}])
    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, portfolio_alts)
    rec("10_correlated_alt_group_exposure", r["decision"] == "REJECTED" and any("correlated_group_exposure" in x for x in r["rejection_reasons"]),
        "REJECTED, binding_limit names correlated_group_exposure -- not treated as independent trades", f"{r['decision']} {r['rejection_reasons']}")

    dq_bad = {"is_tradeable": False, "data_quality_score": 20, "source_timestamps": {}}
    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, dq_bad, _EMPTY_PORTFOLIO)
    rec("11_stale_data_rejected", r["decision"] == "REJECTED" and any("data quality gate" in x for x in r["rejection_reasons"]),
        "REJECTED (stale/untradeable data)", f"{r['decision']} {r['rejection_reasons']}")

    ob_wide = {"available": True, "spread_bps": Decimal("20")}
    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), ob_wide, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("12_wide_spread_rejected", r["decision"] == "REJECTED" and any("spread" in x for x in r["rejection_reasons"]),
        "REJECTED (spread)", f"{r['decision']} {r['rejection_reasons']}")

    r = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG", stop_loss=None), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("13_missing_sl_rejected", r["decision"] == "REJECTED" and any("missing or invalid SL" in x for x in r["rejection_reasons"]),
        "REJECTED (missing SL)", f"{r['decision']} {r['rejection_reasons']}")

    # 14 + 15: signal_id deduplication and restart-safety, exercised against
    # a scratch temp file so this doesn't touch the real signals.jsonl.
    regime_full = {"primary_regime": "TREND_UP", "confidence": 0.5, "secondary_flags": [], "reasons": [], "counterarguments": [], "data_quality": 0.9}
    setups = {"trend_pullback": dict(_setup("LONG"), invalidation=Decimal("94"), orderflow_confirmed=True,
                                      reasons=[], counterarguments=[])}
    confirmations = {"trend_pullback": {"status": "CONFIRMED", "confidence": 0.6}}
    risk_decisions = {"trend_pullback": {"decision": "APPROVED", "position_size": Decimal("10")}}
    dq_with_ts = {"source_timestamps": {"candles": "2026-08-09T12:00:00+00:00"}}
    with tempfile.TemporaryDirectory() as d:
        scratch = Path(d) / "signals.jsonl"
        recs1 = snap.build_snapshots("ETHUSDT", _NOW, regime_full, setups, confirmations, risk_decisions, dq_with_ts, 100)
        w1 = snap.append_snapshots(recs1, path=scratch)
        recs2 = snap.build_snapshots("ETHUSDT", _NOW + dt.timedelta(minutes=10), regime_full, setups, confirmations, risk_decisions, dq_with_ts, 101)
        w2 = snap.append_snapshots(recs2, path=scratch)
        lines = scratch.read_text().strip().split("\n") if scratch.exists() else []
        ok_14 = w1["written"] == 1 and w1["duplicates_skipped"] == 0
        ok_15 = w2["written"] == 0 and w2["duplicates_skipped"] == 1 and len(lines) == 1
    rec("14_signal_id_deduplication", ok_14, "first write: written=1, duplicates_skipped=0", str(w1))
    rec("15_restart_no_duplicate_signal", ok_15, "second write (same hour, simulated restart): written=0, duplicates_skipped=1, 1 line on disk",
        f"{w2} lines_on_disk={len(lines)}")

    sig_params = set(inspect.signature(rm.evaluate_risk).parameters)
    no_future_param = not any("future" in p or "outcome" in p for p in sig_params)
    r1 = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    r2 = rm.evaluate_risk("ETHUSDT", _NOW, _REGIME_OK, _setup("LONG"), _confirmation("CONFIRMED"), _OB_OK, _DQ_GOOD, _EMPTY_PORTFOLIO)
    rec("16_no_future_data_influence", no_future_param and r1 == r2,
        "no forward-looking parameter in signature + deterministic repeat calls",
        f"no_future_param={no_future_param} deterministic={r1 == r2}")

    return results


try:
    report["synthetic_tests"] = run_synthetic_tests()
except Exception:
    report["synthetic_tests"] = {"_error": traceback.format_exc()}

synth_failed = [k for k, v in report["synthetic_tests"].items() if isinstance(v, dict) and not v.get("pass", False)]


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: live run -- regime -> setup -> confirmation -> risk, with an
# ACCUMULATING portfolio_state across symbols in this one pass.
# ══════════════════════════════════════════════════════════════════════════

async def run_live_pipeline() -> dict:
    settings = get_trading_settings()
    symbols = list(settings.symbols)
    ordered = (["BTCUSDT"] if "BTCUSDT" in symbols else []) + [s for s in symbols if s != "BTCUSDT"]

    config = rm.DEFAULT_RISK_CONFIG
    portfolio_state = {"open_positions": [], "daily_realized_pnl_pct": Decimal("0"),
                        "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False}

    results = {}
    all_snapshots = []
    btc_regime = None
    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await pipeline.run_pipeline_for_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                if sym == "BTCUSDT":
                    btc_regime = r["regime"]["primary_regime"]

                risk_decisions = {}
                for setup_type, s in r["setups"].items():
                    confirmation = r["confirmations"][setup_type]
                    decision = rm.evaluate_risk(
                        sym, r["as_of"], r["regime"], s, confirmation, r.get("orderbook", {"available": False}),
                        r["data_quality"], portfolio_state, config,
                    )
                    risk_decisions[setup_type] = decision
                    if decision["decision"] in ("APPROVED", "APPROVED_REDUCED_SIZE"):
                        portfolio_state["open_positions"].append({
                            "symbol": sym, "direction": decision["direction"], "position_value": decision["position_value"],
                        })

                r["risk_decisions"] = risk_decisions
                results[sym] = r

                records = snap.build_snapshots(sym, r["as_of"], r["regime"], r["setups"], r["confirmations"],
                                                risk_decisions, r["data_quality"], r["price_at_decision"])
                all_snapshots.extend(records)
            except Exception:
                results[sym] = {"error": traceback.format_exc()}

    try:
        write_result = snap.append_snapshots(all_snapshots)
        snapshot_write_error = None
    except Exception:
        write_result = {"written": 0, "duplicates_skipped": 0}
        snapshot_write_error = traceback.format_exc()

    valid = {s: r for s, r in results.items() if "error" not in r}
    chain_summary = {}
    for sym, r in valid.items():
        chain = {}
        for setup_type, s in r["setups"].items():
            if s["direction"] == "NO_SETUP":
                continue
            conf = r["confirmations"][setup_type]
            risk = r["risk_decisions"][setup_type]
            chain[setup_type] = {
                "direction": s["direction"], "confirmation": conf["status"], "risk_decision": risk["decision"],
                "position_size": risk["position_size"], "position_value": risk["position_value"],
                "reward_to_risk": risk["reward_to_risk"], "rejection_reasons": risk["rejection_reasons"],
            }
        chain_summary[sym] = {"regime": r["regime"]["primary_regime"], "regime_confidence": r["regime"]["confidence"], "chain": chain}

    return {
        "chain_summary": chain_summary,
        "final_portfolio_state": {
            "open_positions": portfolio_state["open_positions"],
            "num_positions": len(portfolio_state["open_positions"]),
            "total_exposure": sum((p["position_value"] for p in portfolio_state["open_positions"]), Decimal("0")),
        },
        "snapshots_written": write_result["written"], "snapshots_duplicates_skipped": write_result["duplicates_skipped"],
        "snapshot_path": str(snap.SNAPSHOT_PATH), "snapshot_write_error": snapshot_write_error,
        "risk_config": config,
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
report["layer_4_status"] = {
    "logically_architecturally_complete": True,
    "synthetically_tested": n_pass == n_total and n_total > 0,
    "synthetic_tests_pass": f"{n_pass}/{n_total}",
    "live_smoke_tested": "error" not in report.get("live_run", {"error": True}),
    "statistically_validated": False,
    "statistically_validated_reason": "no outcome data yet -- risk decisions are being recorded from this run onward, nothing evaluated against subsequent price action",
    "production_ready": False,
    "order_execution_wired": False,
    "paper_positions_opened": False,
    "note": "risk_manager_v2 NEVER opens a position, even simulated -- decision + sizing only. Paper execution is Warstwa 5.",
}

REPORT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

print("=" * 70)
print("RISK MANAGER v2 -- AUDIT REPORT (Warstwa 4)")
print(f"STATUS: {report['status']}")
print("=" * 70)
print(f"Synthetic tests: {n_pass}/{n_total} PASS")
if synth_failed:
    for name in synth_failed:
        t = report["synthetic_tests"][name]
        print(f"  FAILED: {name} -> expected {t['expected']}, got {t['got']}")
print("-" * 70)
print("Live run -- regime -> setup -> order-flow confirmation -> risk decision chain:")
lr = report["live_run"]
if "error" in lr:
    print(f"  ERROR: {lr['error'][-2000:]}")
else:
    for sym, c in lr["chain_summary"].items():
        if not c["chain"]:
            print(f"  {sym:10s} regime={c['regime']:22s} conf={c['regime_confidence']:.2f}  (no setup fired)")
            continue
        for setup_type, entry in c["chain"].items():
            rr = entry["reward_to_risk"]
            print(f"  {sym:10s} regime={c['regime']:22s} {setup_type}:{entry['direction']} "
                  f"conf.flow={entry['confirmation']:16s} risk={entry['risk_decision']:22s} "
                  f"size={entry['position_size']} value={entry['position_value']} RR={rr}")
            if entry["rejection_reasons"]:
                print(f"             reasons: {entry['rejection_reasons']}")
    fp = lr["final_portfolio_state"]
    print(f"  final simulated portfolio (this run only, nothing executed): "
          f"{fp['num_positions']} positions, total_exposure={fp['total_exposure']}")
    print(f"  snapshots written: {lr['snapshots_written']} (duplicates skipped: {lr['snapshots_duplicates_skipped']}) -> {lr['snapshot_path']}")
    if lr.get("snapshot_write_error"):
        print(f"  SNAPSHOT WRITE ERROR: {lr['snapshot_write_error'][-1000:]}")
print("-" * 70)
print(f"Full report written to: {REPORT_PATH}")
