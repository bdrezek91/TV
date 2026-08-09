"""One-shot audit + validation of setup_detector_v2 ("Warstwa 2").

Two phases, recorded into one JSON report:
  1. Synthetic tests (no DB) -- 14 scenarios covering each of the 4 setup
     types firing correctly, each type correctly returning NO_SETUP against
     an incompatible regime, evidence-gated NO_SETUP within a compatible
     regime (extended trend / mid-range / unresolved squeeze), the
     price-only-mode flag when order flow is unavailable, and the
     confidence-floor collapsing a low-regime-confidence setup to NO_SETUP.
  2. Live run across all tracked symbols, from real DB data -- for each
     symbol, classifies the regime (Warstwa 1) then evaluates all 4 setup
     types against it. Most symbol/setup-type pairs will correctly show
     NO_SETUP (regime incompatible) -- that's the design working, not a
     shortfall. Aggregates: how many setups actually fired, direction
     split, price-only-mode count, regime distribution feeding into this.

This module NEVER places, modifies, or cancels a real order -- setup
detection only, matching the roadmap: "Warstwa 2 ma wykrywać setupy, ale
nie wykonywać transakcji."

Persistence is JSON only (artifacts/research/setup_detector/) -- no DB
writes, no migration, same convention as audit_regime_classifier.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import traceback
from collections import Counter
from decimal import Decimal
from pathlib import Path

REPORT_DIR = Path("/app/artifacts/research/setup_detector")
REPORT_PATH = REPORT_DIR / "latest_report.json"

report: dict = {
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "synthetic_tests": {},
    "live_run": {},
}

# ── Import check (fatal if this fails -- that IS the point) ────────────────
from tradingview_mcp.core.analysis import setup_detector_v2 as sd  # noqa: E402
from tradingview_mcp.core.services import setup_service_v2 as ss  # noqa: E402
from tradingview_mcp.core.config.trading_settings import get_trading_settings  # noqa: E402
from tradingview_mcp.core.database.session import session_scope  # noqa: E402

print("IMPORT OK: setup_detector_v2 / setup_service_v2 imported cleanly.", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: synthetic tests (no DB)
# ══════════════════════════════════════════════════════════════════════════

_NOW = dt.datetime.now(dt.timezone.utc)


def _regime(primary, confidence=0.6, flags=None):
    return {"primary_regime": primary, "confidence": confidence, "secondary_flags": flags or []}


def _pa(available=True, last_close=100, ema20=None, ema50=None, atr=Decimal("2"),
        support=None, resistance=None, position_in_range=None):
    if not available:
        return {"available": False}
    return {"available": True, "last_close": Decimal(str(last_close)), "ema20": ema20, "ema50": ema50,
            "atr": atr, "support": support, "resistance": resistance, "position_in_range": position_in_range}


def _vol(available=True, cvd_slope_15m=None, volume_zscore=None):
    return {"available": False} if not available else \
        {"available": True, "cvd_slope_15m": cvd_slope_15m, "volume_zscore": volume_zscore}


def _fut(available=True, oi_change_1h_pct=None):
    return {"available": False} if not available else {"available": True, "oi_change_1h_pct": oi_change_1h_pct}


def _liq(available=True, cascade_detected=True, imbalance_15m=None):
    return {"available": available, "cascade_detected": cascade_detected, "imbalance_15m": imbalance_15m}


_TF_EMPTY = {"4h": _pa(False), "1h": _pa(False), "15m": _pa(False)}


def run_synthetic_tests() -> dict:
    results = {}

    def check(name, setup_type, expected_direction, r, expected_regime_compatible=None):
        got = r["setups"][setup_type]
        ok = got["direction"] == expected_direction
        if expected_regime_compatible is not None:
            ok = ok and got["regime_compatible"] == expected_regime_compatible
        results[name] = {
            "pass": ok, "setup_type": setup_type, "expected": expected_direction, "got": got["direction"],
            "confidence": got.get("confidence", 0), "price_only_mode": got.get("price_only_mode"),
        }

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=104, ema20=105, ema50=100, atr=Decimal("2"), support=Decimal("98"), resistance=Decimal("112"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_UP", 0.6), tf, _vol(True, cvd_slope_15m=Decimal("10")), {"available": False}, _fut(False), _liq(False))
    check("1_pullback_long_fires", "trend_pullback", "LONG", r)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=96, ema20=95, ema50=100, atr=Decimal("2"), support=Decimal("88"), resistance=Decimal("102"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_DOWN", 0.6), tf, _vol(True, cvd_slope_15m=Decimal("-10")), {"available": False}, _fut(False), _liq(False))
    check("2_pullback_short_fires", "trend_pullback", "SHORT", r)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=130, ema20=105, ema50=100, atr=Decimal("2"), support=Decimal("98"), resistance=Decimal("135"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_UP", 0.6), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("3_pullback_extended_no_setup", "trend_pullback", "NO_SETUP", r, expected_regime_compatible=True)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=104, ema20=105, ema50=100)})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("RANGE", 0.5), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("4_pullback_wrong_regime", "trend_pullback", "NO_SETUP", r, expected_regime_compatible=False)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=112, atr=Decimal("2"), support=Decimal("95"), resistance=Decimal("110"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("BREAKOUT_UP", 0.55), tf, _vol(True, volume_zscore=Decimal("2.0")), {"available": False}, _fut(True, oi_change_1h_pct=Decimal("3")), _liq(False))
    check("5_breakout_long_fires", "breakout", "LONG", r)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=100, atr=Decimal("1"), support=Decimal("97"), resistance=Decimal("103"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("SQUEEZE", 0.5), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("6_breakout_squeeze_no_direction", "breakout", "NO_SETUP", r, expected_regime_compatible=True)

    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_UP", 0.6), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("7_breakout_wrong_regime", "breakout", "NO_SETUP", r, expected_regime_compatible=False)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=91, atr=Decimal("2"), support=Decimal("90"), resistance=Decimal("110"), position_in_range=Decimal("0.05"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("RANGE", 0.5), tf, _vol(True, cvd_slope_15m=Decimal("1")), {"available": False}, _fut(False), _liq(False))
    check("8_mean_reversion_long_fires", "mean_reversion", "LONG", r)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=100, atr=Decimal("2"), support=Decimal("90"), resistance=Decimal("110"), position_in_range=Decimal("0.5"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("RANGE", 0.5), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("9_mean_reversion_mid_range_no_setup", "mean_reversion", "NO_SETUP", r, expected_regime_compatible=True)

    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_UP", 0.6), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("10_mean_reversion_wrong_regime", "mean_reversion", "NO_SETUP", r, expected_regime_compatible=False)

    tf = dict(_TF_EMPTY, **{"15m": _pa(last_close=95, atr=Decimal("1.5"), support=Decimal("93"), resistance=Decimal("105"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("PANIC_LIQUIDATION_LONGS", 0.6), tf, _vol(False), {"available": False}, _fut(False), _liq(True, True, imbalance_15m=Decimal("-0.7")))
    check("11_liq_reversal_long_fires", "liquidation_reversal", "LONG", r)

    r = sd.detect_setups("BTCUSDT", _NOW, _regime("RANGE", 0.5), tf, _vol(False), {"available": False}, _fut(False), _liq(True, True, imbalance_15m=Decimal("-0.7")))
    check("12_liq_reversal_wrong_regime", "liquidation_reversal", "NO_SETUP", r, expected_regime_compatible=False)

    tf = dict(_TF_EMPTY, **{"1h": _pa(last_close=104, ema20=105, ema50=100, atr=Decimal("2"), support=Decimal("98"), resistance=Decimal("112"))})
    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_UP", 0.6), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    got = r["setups"]["trend_pullback"]
    results["13_price_only_mode_flag"] = {
        "pass": got["direction"] == "LONG" and got["price_only_mode"] is True and got["orderflow_confirmed"] is False,
        "setup_type": "trend_pullback", "expected": "LONG,price_only=True",
        "got": f"{got['direction']},price_only={got['price_only_mode']}", "confidence": got.get("confidence", 0),
    }

    r = sd.detect_setups("BTCUSDT", _NOW, _regime("TREND_UP", 0.20), tf, _vol(False), {"available": False}, _fut(False), _liq(False))
    check("14_low_regime_confidence_no_setup", "trend_pullback", "NO_SETUP", r, expected_regime_compatible=True)

    return results


try:
    report["synthetic_tests"] = run_synthetic_tests()
except Exception:
    report["synthetic_tests"] = {"_error": traceback.format_exc()}

synth_failed = [k for k, v in report["synthetic_tests"].items() if isinstance(v, dict) and not v.get("pass", False)]


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: live run across all tracked symbols
# ══════════════════════════════════════════════════════════════════════════

async def run_live_setups() -> dict:
    settings = get_trading_settings()
    symbols = list(settings.symbols)
    ordered = (["BTCUSDT"] if "BTCUSDT" in symbols else []) + [s for s in symbols if s != "BTCUSDT"]

    results = {}
    btc_regime = None
    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await ss.detect_setups_for_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                results[sym] = r
                if sym == "BTCUSDT":
                    btc_regime = r["regime_full"]["primary_regime"]
            except Exception:
                results[sym] = {"error": traceback.format_exc()}

    valid = {s: r for s, r in results.items() if "error" not in r}
    fired = []  # (symbol, setup_type, direction, confidence, price_only_mode)
    for sym, r in valid.items():
        for setup_type, s in r["setups"].items():
            if s["direction"] != "NO_SETUP":
                fired.append((sym, setup_type, s["direction"], s["confidence"], s["price_only_mode"]))

    direction_counts = Counter(f[2] for f in fired)
    setup_type_counts = Counter(f[1] for f in fired)
    price_only_count = sum(1 for f in fired if f[4])
    regime_distribution = Counter(r["regime_context"]["primary_regime"] for r in valid.values())

    return {
        "symbols": results,
        "setups_fired": [{"symbol": s, "setup_type": t, "direction": d, "confidence": c, "price_only_mode": p}
                          for s, t, d, c, p in fired],
        "num_setups_fired": len(fired),
        "num_symbols_evaluated": len(valid),
        "direction_distribution": dict(direction_counts),
        "setup_type_distribution": dict(setup_type_counts),
        "price_only_mode_count": price_only_count,
        "regime_distribution_context": dict(regime_distribution),
    }


try:
    report["live_run"] = asyncio.run(run_live_setups())
except Exception:
    report["live_run"] = {"error": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════════
# Status -- explicit, not implied. Setup detection only, no execution.
# ══════════════════════════════════════════════════════════════════════════

n_pass = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict) and v.get("pass"))
n_total = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict))

report["status"] = "IMPLEMENTATION_COMPLETE_VALIDATION_PENDING"
report["layer_2_status"] = {
    "logically_architecturally_complete": True,
    "synthetically_tested": n_pass == n_total and n_total > 0,
    "synthetic_tests_pass": f"{n_pass}/{n_total}",
    "live_smoke_tested": "error" not in report.get("live_run", {"error": True}),
    "statistically_validated": False,
    "statistically_validated_reason": "no historical setup-outcome backtest yet -- depends on Warstwa 1's own historical backtest, which itself needs more trade_aggregates history to be meaningful",
    "production_ready": False,
    "order_execution_wired": False,
    "note": "setup_detector_v2 NEVER places, modifies, or cancels an order -- detection/description only",
}

REPORT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

print("=" * 70)
print("SETUP DETECTOR v2 -- AUDIT REPORT (Warstwa 2)")
print(f"STATUS: {report['status']}")
print("=" * 70)
print(f"Synthetic tests: {n_pass}/{n_total} PASS")
if synth_failed:
    for name in synth_failed:
        t = report["synthetic_tests"][name]
        print(f"  FAILED: {name} ({t['setup_type']}) -> expected {t['expected']}, got {t['got']}")
print("-" * 70)
print("Live run across tracked symbols:")
lr = report["live_run"]
if "error" in lr:
    print(f"  ERROR: {lr['error'][-2000:]}")
else:
    for sym, r in lr["symbols"].items():
        if "error" in r:
            print(f"  {sym:10s} ERROR: {r['error'][-2000:]}")
            continue
        rc_ctx = r["regime_context"]
        fired_here = [f"{t}:{s['direction']}({s['confidence']})" for t, s in r["setups"].items() if s["direction"] != "NO_SETUP"]
        print(f"  {sym:10s} regime={rc_ctx['primary_regime']:22s} conf={rc_ctx['confidence']:.2f} "
              f"setups_fired={fired_here if fired_here else '(none)'}")
    print(f"  total setups fired: {lr['num_setups_fired']} across {lr['num_symbols_evaluated']} symbols")
    print(f"  direction distribution: {lr['direction_distribution']}")
    print(f"  setup_type distribution: {lr['setup_type_distribution']}")
    print(f"  price_only_mode count: {lr['price_only_mode_count']}")
    print(f"  regime distribution (context): {lr['regime_distribution_context']}")
print("-" * 70)
print(f"Full report written to: {REPORT_PATH}")
