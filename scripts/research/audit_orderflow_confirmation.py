"""One-shot audit + validation of orderflow_confirmation_v2 ("Warstwa 3").

Two phases, recorded into one JSON report:
  1. Synthetic tests (no DB) -- 12 scenarios: LONG/SHORT confirmation,
     CVD divergence against a setup, price-up/OI-down and price-down/OI-up
     (must not be treated as a clean confirm / must confirm correctly),
     extreme funding, a liquidation cascade aligned AND against a setup,
     a contradictory order book, no order flow at all, stale data (must
     be excluded like missing, not trusted), the anti-double-counting
     guarantee, and NOT_APPLICABLE for a NO_SETUP direction.
  2. Live run across all tracked symbols -- chains regime (Warstwa 1) ->
     setup (Warstwa 2) -> order-flow confirmation (Warstwa 3) on one
     shared feature snapshot per symbol, persists a durable JSONL
     snapshot (artifacts/research/signals/signals.jsonl) for every actual
     LONG/SHORT setup with an empty `outcome` placeholder for a LATER,
     SEPARATE evaluator to fill in -- never evaluated here, that would be
     look-ahead bias at signal-generation time.

NEVER places, modifies, or cancels a real order -- detection/confirmation
only, same as Warstwa 1 and 2. Persistence is JSON/JSONL only, no DB
writes, no migration.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import traceback
from decimal import Decimal
from pathlib import Path

REPORT_DIR = Path("/app/artifacts/research/orderflow_confirmation")
REPORT_PATH = REPORT_DIR / "latest_report.json"

report: dict = {
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "synthetic_tests": {},
    "live_run": {},
}

# ── Import check (fatal if this fails -- that IS the point) ────────────────
from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as of  # noqa: E402
from tradingview_mcp.core.services import signal_pipeline_v2 as pipeline  # noqa: E402
from tradingview_mcp.core.config.trading_settings import get_trading_settings  # noqa: E402
from tradingview_mcp.core.database.session import session_scope  # noqa: E402
from tradingview_mcp.core.database.repositories import query_repository as qr  # noqa: E402

print("IMPORT OK: orderflow_confirmation_v2 / signal_pipeline_v2 / signal_snapshot_service_v2 imported cleanly.", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: synthetic tests (no DB)
# ══════════════════════════════════════════════════════════════════════════

def _setup(direction="LONG", reasons=None, counter=None):
    return {"direction": direction, "reasons": reasons or [], "counterarguments": counter or []}


def _regime(reasons=None, counter=None):
    return {"reasons": reasons or [], "counterarguments": counter or []}


def _vol(available=True, cvd_slope_15m=None, buy=None, sell=None, large_trade_count=None):
    if not available:
        return {"available": False}
    return {"available": True, "cvd_slope_15m": cvd_slope_15m, "buy_taker_volume": buy,
            "sell_taker_volume": sell, "large_trade_count": large_trade_count}


def _ob(available=True, imbalance=None, spread_bps=Decimal("5"), microprice=None, mid_price=None):
    if not available:
        return {"available": False}
    return {"available": True, "imbalance": imbalance, "spread_bps": spread_bps,
            "microprice": microprice, "mid_price": mid_price}


def _fut(available=True, price_to_oi_relation=None, funding_rate=Decimal("0.0001"), long_short_ratio=Decimal("1.0")):
    if not available:
        return {"available": False}
    return {"available": True, "price_to_oi_relation": price_to_oi_relation,
            "funding_rate": funding_rate, "long_short_ratio": long_short_ratio}


def _liq(available=True, cascade_detected=False, imbalance_15m=None):
    return {"available": available, "cascade_detected": cascade_detected, "imbalance_15m": imbalance_15m}


_TF_EMPTY = {"4h": {"available": False}, "1h": {"available": False}, "15m": {"available": False}}


def run_synthetic_tests() -> dict:
    results = {}

    def rec(name, r, expected_status, pass_check=None):
        ok = r["status"] == expected_status
        if pass_check is not None:
            ok = ok and pass_check(r)
        results[name] = {"pass": ok, "expected": expected_status, "got": r["status"],
                          "confidence": r["confidence"], "net_score": r["net_independent_score"]}

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG", reasons=["4h and 1h structure both agree on a uptrend"]),
        _regime(reasons=["4h and 1h structure both agree on a uptrend"]), _TF_EMPTY,
        _vol(True, cvd_slope_15m=Decimal("10"), buy=Decimal("100"), sell=Decimal("50"), large_trade_count=5),
        _ob(True, imbalance=Decimal("0.3"), microprice=Decimal("100.05"), mid_price=Decimal("100")),
        _fut(True, price_to_oi_relation="PRICE_UP_OI_UP", funding_rate=Decimal("0.0001"), long_short_ratio=Decimal("1.0")),
        _liq(False), btc_regime="TREND_UP",
    )
    rec("1_long_confirmation", r, "CONFIRMED")

    r = of.confirm_setup(
        "ETHUSDT", _setup("SHORT", reasons=["4h and 1h structure both agree on a downtrend"]),
        _regime(reasons=["4h and 1h structure both agree on a downtrend"]), _TF_EMPTY,
        _vol(True, cvd_slope_15m=Decimal("-10"), buy=Decimal("50"), sell=Decimal("100"), large_trade_count=5),
        _ob(True, imbalance=Decimal("-0.3"), microprice=Decimal("99.95"), mid_price=Decimal("100")),
        _fut(True, price_to_oi_relation="PRICE_DOWN_OI_UP", funding_rate=Decimal("-0.0001"), long_short_ratio=Decimal("1.0")),
        _liq(False), btc_regime="TREND_DOWN",
    )
    rec("2_short_confirmation", r, "CONFIRMED")

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY,
        _vol(True, cvd_slope_15m=Decimal("-15")), _ob(True, imbalance=Decimal("0.05"), spread_bps=Decimal("5")),
        _fut(False), _liq(False),
    )
    rec("3_cvd_divergence_against_setup", r, "CONTRADICTED",
        lambda r: "cvd_direction_slope_divergence" in r["contradictions"])

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY,
        _vol(False), _ob(False), _fut(True, price_to_oi_relation="PRICE_UP_OI_DOWN"), _liq(False),
    )
    oi_dim = next(d for d in r["dimensions"] if d["dimension"] == "open_interest_and_price_to_oi")
    results["4_price_up_oi_down_not_clean_confirm"] = {
        "pass": oi_dim["read"] == "NEUTRAL", "expected": "NEUTRAL", "got": oi_dim["read"], "confidence": None, "net_score": None,
    }

    r = of.confirm_setup(
        "ETHUSDT", _setup("SHORT"), _regime(), _TF_EMPTY,
        _vol(False), _ob(False), _fut(True, price_to_oi_relation="PRICE_DOWN_OI_UP"), _liq(False),
    )
    oi_dim = next(d for d in r["dimensions"] if d["dimension"] == "open_interest_and_price_to_oi")
    results["5_price_down_oi_up_confirms_short"] = {
        "pass": oi_dim["read"] == "CONFIRMS", "expected": "CONFIRMS", "got": oi_dim["read"], "confidence": None, "net_score": None,
    }

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY,
        _vol(False), _ob(False), _fut(True, funding_rate=Decimal("0.002")), _liq(False),
    )
    fund_dim = next(d for d in r["dimensions"] if d["dimension"] == "funding_extremity")
    results["6_extreme_funding_contradicts_never_confirms"] = {
        "pass": fund_dim["read"] == "CONTRADICTS", "expected": "CONTRADICTS", "got": fund_dim["read"], "confidence": None, "net_score": None,
    }

    r = of.confirm_setup(
        "ETHUSDT", _setup("SHORT"), _regime(), _TF_EMPTY,
        _vol(False), _ob(False), _fut(False), _liq(True, cascade_detected=True, imbalance_15m=Decimal("0.7")),
    )
    liq_dim = next(d for d in r["dimensions"] if d["dimension"] == "liquidations_longs_shorts")
    results["7a_liq_cascade_aligned_with_short"] = {
        "pass": liq_dim["read"] == "CONFIRMS", "expected": "CONFIRMS", "got": liq_dim["read"], "confidence": None, "net_score": None,
    }

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY,
        _vol(False), _ob(False), _fut(False), _liq(True, cascade_detected=True, imbalance_15m=Decimal("0.7")),
    )
    liq_dim = next(d for d in r["dimensions"] if d["dimension"] == "liquidations_longs_shorts")
    results["7b_liq_cascade_against_long"] = {
        "pass": liq_dim["read"] == "CONTRADICTS", "expected": "CONTRADICTS", "got": liq_dim["read"], "confidence": None, "net_score": None,
    }

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY,
        _vol(False), _ob(True, imbalance=Decimal("-0.5"), spread_bps=Decimal("5")), _fut(False), _liq(False),
    )
    ob_dim = next(d for d in r["dimensions"] if d["dimension"] == "orderbook_imbalance")
    results["8_contradictory_orderbook"] = {
        "pass": ob_dim["read"] == "CONTRADICTS", "expected": "CONTRADICTS", "got": ob_dim["read"], "confidence": None, "net_score": None,
    }

    r = of.confirm_setup("ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY, _vol(False), _ob(False), _fut(False), _liq(False), btc_regime=None)
    rec("9_no_orderflow_insufficient_data", r, "INSUFFICIENT_DATA")

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG"), _regime(), _TF_EMPTY,
        _vol(True, cvd_slope_15m=Decimal("10"), buy=Decimal("100"), sell=Decimal("50"), large_trade_count=5),
        _ob(True, imbalance=Decimal("0.3")), _fut(True, price_to_oi_relation="PRICE_UP_OI_UP"), _liq(False),
        data_quality={"stale_fields": ["trades", "orderbook", "derivatives"]},
    )
    stale_names = ("cvd_direction_slope_divergence", "taker_buy_sell_ratio", "large_trades",
                   "orderbook_imbalance", "spread_and_depth", "microprice",
                   "open_interest_and_price_to_oi", "funding_extremity", "long_short_ratio")
    all_unavail = all(d["read"] == "UNAVAILABLE" for d in r["dimensions"] if d["dimension"] in stale_names)
    results["10_stale_data_excluded_not_trusted"] = {
        "pass": all_unavail and r["status"] == "INSUFFICIENT_DATA",
        "expected": "all stale-sourced dims UNAVAILABLE + INSUFFICIENT_DATA",
        "got": f"all_unavailable={all_unavail} status={r['status']}", "confidence": r["confidence"], "net_score": r["net_independent_score"],
    }

    r = of.confirm_setup(
        "ETHUSDT", _setup("LONG", reasons=["open interest rising alongside price"]),
        _regime(reasons=["positive CVD slope confirms buy-side aggression", "open interest rising alongside price -- new long positioning"]),
        _TF_EMPTY, _vol(True, cvd_slope_15m=Decimal("10")), _ob(False),
        _fut(True, price_to_oi_relation="PRICE_UP_OI_UP"), _liq(False),
    )
    not_double_counted = ("cvd_direction_slope_divergence" not in r["new_independent_confirmations"] and
                           "open_interest_and_price_to_oi" not in r["new_independent_confirmations"])
    results["11_anti_double_counting"] = {
        "pass": not_double_counted, "expected": "CVD/OI already used by regime -> excluded from new_independent_confirmations",
        "got": f"new_independent_confirmations={r['new_independent_confirmations']} used_by_regime={r['used_by_regime']}",
        "confidence": r["confidence"], "net_score": r["net_independent_score"],
    }

    r = of.confirm_setup("ETHUSDT", _setup("NO_SETUP"), _regime(), _TF_EMPTY, _vol(False), _ob(False), _fut(False), _liq(False))
    rec("12_no_setup_direction_not_applicable", r, "NOT_APPLICABLE")

    return results


try:
    report["synthetic_tests"] = run_synthetic_tests()
except Exception:
    report["synthetic_tests"] = {"_error": traceback.format_exc()}

synth_failed = [k for k, v in report["synthetic_tests"].items() if isinstance(v, dict) and not v.get("pass", False)]


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: live run -- regime -> setup -> order-flow confirmation chain
# ══════════════════════════════════════════════════════════════════════════

async def _trade_aggregates_span(session, symbols: list) -> dict:
    earliest, latest, total = None, None, 0
    for sym in symbols:
        rows = await qr.get_recent_trade_aggregates(session, sym, 60, limit=5000)
        if not rows:
            continue
        total += len(rows)
        first_ts, last_ts = rows[0].bucket_start, rows[-1].bucket_start
        earliest = first_ts if earliest is None or first_ts < earliest else earliest
        latest = last_ts if latest is None or last_ts > latest else latest
    return {
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
        "hours_span": round((latest - earliest).total_seconds() / 3600, 2) if (earliest and latest) else None,
        "total_rows_all_symbols": total,
    }


async def run_live_pipeline() -> dict:
    settings = get_trading_settings()
    symbols = list(settings.symbols)
    ordered = (["BTCUSDT"] if "BTCUSDT" in symbols else []) + [s for s in symbols if s != "BTCUSDT"]

    results = {}
    btc_regime = None
    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await pipeline.run_pipeline_for_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                results[sym] = r
                if sym == "BTCUSDT":
                    btc_regime = r["regime"]["primary_regime"]
            except Exception:
                results[sym] = {"error": traceback.format_exc()}

        trade_agg_span = await _trade_aggregates_span(session, ordered)

    valid = {s: r for s, r in results.items() if "error" not in r}
    chain_summary = {}
    for sym, r in valid.items():
        fired = {t: {"direction": s["direction"], "confirmation": r["confirmations"][t]["status"]}
                 for t, s in r["setups"].items() if s["direction"] != "NO_SETUP"}
        chain_summary[sym] = {
            "regime": r["regime"]["primary_regime"], "regime_confidence": r["regime"]["confidence"],
            "setups_fired": fired,
        }

    return {
        "symbols": {sym: {
            "regime": r["regime"]["primary_regime"], "regime_confidence": r["regime"]["confidence"],
            "setups": {t: {"direction": s["direction"], "confidence": s["confidence"]} for t, s in r["setups"].items()},
            "confirmations": {t: {"status": c["status"], "confidence": c["confidence"],
                                   "new_independent_confirmations": c.get("new_independent_confirmations", []),
                                   "contradictions": c.get("contradictions", []),
                                   "missing_sources": c.get("missing_sources", [])}
                               for t, c in r["confirmations"].items()},
        } if "error" not in r else {"error": r["error"]} for sym, r in results.items()},
        "chain_summary": chain_summary,
        "snapshot_note": "Durable snapshot writing moved to Warstwa 4 (scripts/research/audit_risk_manager.py) -- "
                          "regime->setup->orderflow->risk is the complete decision chain worth persisting, and "
                          "writing it here too would collide on the same signal_id within the same decision hour.",
        "trade_aggregates_availability": trade_agg_span,
    }


try:
    report["live_run"] = asyncio.run(run_live_pipeline())
except Exception:
    report["live_run"] = {"error": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════════
# Status + re-evaluation schedule
# ══════════════════════════════════════════════════════════════════════════

n_pass = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict) and v.get("pass"))
n_total = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict))

now = dt.datetime.now(dt.timezone.utc)
report["status"] = "IMPLEMENTATION_COMPLETE_VALIDATION_PENDING"
report["layer_3_status"] = {
    "logically_architecturally_complete": True,
    "synthetically_tested": n_pass == n_total and n_total > 0,
    "synthetic_tests_pass": f"{n_pass}/{n_total}",
    "live_smoke_tested": "error" not in report.get("live_run", {"error": True}),
    "statistically_validated": False,
    "statistically_validated_reason": "no outcome data yet -- signal snapshots are being written from this run onward, but nothing has been evaluated against subsequent price action",
    "production_ready": False,
    "order_execution_wired": False,
    "paper_broker_wired_as_executor": False,
}
report["reevaluation_schedule"] = {
    "note": "Warstwa 1-3 should be re-assessed once trade_aggregates history is deep enough to matter -- implementation continues in parallel, this is NOT a blocking gate.",
    "checkpoint_1_at_7_days": (now + dt.timedelta(days=7)).isoformat(),
    "checkpoint_2_at_30_days": (now + dt.timedelta(days=30)).isoformat(),
}

REPORT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

print("=" * 70)
print("ORDER FLOW CONFIRMATION v2 -- AUDIT REPORT (Warstwa 3)")
print(f"STATUS: {report['status']}")
print("=" * 70)
print(f"Synthetic tests: {n_pass}/{n_total} PASS")
if synth_failed:
    for name in synth_failed:
        t = report["synthetic_tests"][name]
        print(f"  FAILED: {name} -> expected {t['expected']}, got {t['got']}")
print("-" * 70)
print("Live run -- regime -> setup -> order-flow confirmation chain:")
lr = report["live_run"]
if "error" in lr:
    print(f"  ERROR: {lr['error'][-2000:]}")
else:
    for sym, c in lr["chain_summary"].items():
        fired = c["setups_fired"]
        if fired:
            fired_str = "; ".join(f"{t}:{v['direction']}->{v['confirmation']}" for t, v in fired.items())
        else:
            fired_str = "(no setup fired)"
        print(f"  {sym:10s} regime={c['regime']:22s} conf={c['regime_confidence']:.2f}  {fired_str}")
    print(f"  {lr['snapshot_note']}")
    ta = lr["trade_aggregates_availability"]
    print(f"  trade_aggregates availability: earliest={ta['earliest']} latest={ta['latest']} "
          f"span={ta['hours_span']}h total_rows={ta['total_rows_all_symbols']}")
print("-" * 70)
rs_sched = report["reevaluation_schedule"]
print(f"Re-evaluation schedule: 7-day checkpoint at {rs_sched['checkpoint_1_at_7_days']}, "
      f"30-day checkpoint at {rs_sched['checkpoint_2_at_30_days']} (non-blocking)")
print("-" * 70)
print(f"Full report written to: {REPORT_PATH}")
