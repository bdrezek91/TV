"""One-shot audit + validation of regime_classifier_v2 ("Warstwa 1").

Three phases, all recorded into one JSON report:
  1. Synthetic tests (no DB) -- the 12 scenarios from the roadmap plus a
     hysteresis check, run directly against the pure classifier.
  2. Live snapshot across all tracked symbols, from real DB data. BTCUSDT
     is classified first so every altcoin can be given real BTC context;
     also checks the pairs aren't all just parroting the same regime.
  3. Historical replay per symbol using only already-persisted candle/
     derivatives/liquidation/trade/orderbook history, walking forward one
     4h candle at a time. LOOK-AHEAD GUARD: at each step every input window
     is sliced to timestamps <= that step's cutoff -- nothing after the
     cutoff is visible to the classifier itself. Forward price data is used
     ONLY afterwards, to evaluate what happened *after* a given regime call,
     never to produce the call.

Persistence for this validation run is JSON only (artifacts/research/
regime_classifier/) -- no DB writes, no migration. See regime_service_v2.py
docstring for why: this is additive research code, not yet wired into the
production Strategy Engine.
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

REPORT_DIR = Path("/app/artifacts/research/regime_classifier")
REPORT_PATH = REPORT_DIR / "latest_report.json"

report: dict = {
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "synthetic_tests": {},
    "live_snapshot": {},
    "historical_backtest": {},
}

# ── Import check (fatal if this fails -- that IS the point) ────────────────
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc  # noqa: E402
from tradingview_mcp.core.services import regime_service_v2 as rs  # noqa: E402
from tradingview_mcp.core.analysis import feature_engine as fe  # noqa: E402
from tradingview_mcp.core.config.trading_settings import get_trading_settings  # noqa: E402
from tradingview_mcp.core.database.session import session_scope  # noqa: E402
from tradingview_mcp.core.database.repositories import query_repository as qr  # noqa: E402

print("IMPORT OK: regime_classifier_v2 / regime_service_v2 imported cleanly.", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: synthetic tests (no DB)
# ══════════════════════════════════════════════════════════════════════════

def _pa(available=True, structure="UNKNOWN", ema20=None, ema50=None, ema200=None,
        last_close=100, atr_pct=Decimal("1.0"), position_in_range=None, pct_change=Decimal("0")):
    if not available:
        return {"available": False}
    return {"available": True, "structure": structure, "ema20": ema20, "ema50": ema50, "ema200": ema200,
            "last_close": last_close, "atr_pct": atr_pct, "position_in_range": position_in_range,
            "pct_change": pct_change}


def _vol(available=True, cvd_slope_15m=None, volume_zscore=None):
    return {"available": False} if not available else \
        {"available": True, "cvd_slope_15m": cvd_slope_15m, "volume_zscore": volume_zscore}


def _ob(available=True, spread_bps=Decimal("5")):
    return {"available": False} if not available else {"available": True, "spread_bps": spread_bps, "is_consistent": True}


def _fut(available=True, price_to_oi_relation=None, oi_change_1h_pct=None, funding_rate=Decimal("0.0001")):
    return {"available": False} if not available else \
        {"available": True, "price_to_oi_relation": price_to_oi_relation,
         "oi_change_1h_pct": oi_change_1h_pct, "funding_rate": funding_rate}


def _liq(available=False, cascade_detected=False, imbalance_15m=None):
    return {"available": available, "cascade_detected": cascade_detected, "imbalance_15m": imbalance_15m}


_DQ_GOOD = {"data_quality_score": 95, "is_tradeable": True, "missing_fields": [], "stale_fields": []}
_NOW = dt.datetime.now(dt.timezone.utc)


def run_synthetic_tests() -> dict:
    results = {}

    def check(name, expected, tf, volume=None, orderbook=None, futures=None,
              liquidations=None, data_quality=None, btc_regime=None, previous_state=None):
        r = rc.classify_from_features(
            "TESTUSDT", _NOW, tf,
            volume or _vol(False), orderbook or _ob(), futures or _fut(False),
            liquidations or _liq(), data_quality or _DQ_GOOD,
            btc_regime=btc_regime, previous_state=previous_state,
        )
        ok = r["primary_regime"] == expected
        results[name] = {
            "pass": ok, "expected": expected, "got": r["primary_regime"],
            "confidence": r["confidence"], "warnings": r["warnings"],
        }
        return r

    check("1_stable_uptrend", "TREND_UP",
          tf={"4h": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112),
              "1h": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112),
              "15m": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112)},
          volume=_vol(True, cvd_slope_15m=Decimal("50")),
          futures=_fut(True, price_to_oi_relation="PRICE_UP_OI_UP", oi_change_1h_pct=Decimal("2")))

    check("2_stable_downtrend", "TREND_DOWN",
          tf={"4h": _pa(structure="LH_LL", ema20=90, ema50=100, ema200=110, last_close=88),
              "1h": _pa(structure="LH_LL", ema20=90, ema50=100, ema200=110, last_close=88),
              "15m": _pa(structure="LH_LL", ema20=90, ema50=100, ema200=110, last_close=88)},
          volume=_vol(True, cvd_slope_15m=Decimal("-50")),
          futures=_fut(True, price_to_oi_relation="PRICE_DOWN_OI_UP", oi_change_1h_pct=Decimal("2")))

    check("3_consolidation", "RANGE",
          tf={"4h": _pa(atr_pct=Decimal("1.0")), "1h": _pa(atr_pct=Decimal("1.0")), "15m": _pa(atr_pct=Decimal("1.0"))},
          volume=_vol(True, cvd_slope_15m=Decimal("0")), futures=_fut(True, oi_change_1h_pct=Decimal("0")))

    check("4_squeeze", "SQUEEZE",
          tf={"4h": _pa(structure="CONTRACTING", atr_pct=Decimal("0.3")),
              "1h": _pa(structure="CONTRACTING", atr_pct=Decimal("0.3")),
              "15m": _pa(structure="CONTRACTING", atr_pct=Decimal("0.3"))},
          volume=_vol(True, cvd_slope_15m=Decimal("0.1")), futures=_fut(True, oi_change_1h_pct=Decimal("3")))

    check("5_breakout_up", "BREAKOUT_UP",
          tf={"4h": _pa(structure="EXPANDING", atr_pct=Decimal("2.0")),
              "1h": _pa(structure="EXPANDING", atr_pct=Decimal("2.0")),
              "15m": _pa(structure="EXPANDING", atr_pct=Decimal("2.0"), position_in_range=Decimal("0.97"))},
          volume=_vol(True, cvd_slope_15m=Decimal("40"), volume_zscore=Decimal("2.2")),
          futures=_fut(True, oi_change_1h_pct=Decimal("4")))

    check("6_false_breakout", "RANGE",
          tf={"4h": _pa(structure="EXPANDING", atr_pct=Decimal("1.0")),
              "1h": _pa(structure="EXPANDING", atr_pct=Decimal("1.0")),
              "15m": _pa(structure="EXPANDING", atr_pct=Decimal("1.0"), position_in_range=Decimal("0.95"))},
          volume=_vol(True, cvd_slope_15m=Decimal("-5"), volume_zscore=Decimal("-0.5")),
          futures=_fut(True, oi_change_1h_pct=Decimal("-1")))

    check("7_panic_liquidation_longs", "PANIC_LIQUIDATION_LONGS",
          tf={"4h": _pa(atr_pct=Decimal("4")), "1h": _pa(atr_pct=Decimal("4")),
              "15m": _pa(atr_pct=Decimal("4"), pct_change=Decimal("-3.5"))},
          liquidations=_liq(True, cascade_detected=True, imbalance_15m=Decimal("-0.7")))

    check("8_panic_liquidation_shorts", "PANIC_LIQUIDATION_SHORTS",
          tf={"4h": _pa(atr_pct=Decimal("4")), "1h": _pa(atr_pct=Decimal("4")),
              "15m": _pa(atr_pct=Decimal("4"), pct_change=Decimal("3.5"))},
          liquidations=_liq(True, cascade_detected=True, imbalance_15m=Decimal("0.7")))

    check("9_low_liquidity", "LOW_LIQUIDITY",
          tf={"4h": _pa(), "1h": _pa(), "15m": _pa()}, orderbook=_ob(True, spread_bps=Decimal("20")))

    check("10_unstable_stale_data", "UNSTABLE_DATA",
          tf={"4h": _pa(structure="HH_HL", ema20=110, ema50=100, last_close=112),
              "1h": _pa(available=False), "15m": _pa(available=False)},
          data_quality={"data_quality_score": 20, "is_tradeable": False, "missing_fields": ["trades"], "stale_fields": []})

    check("11_conflicting_intervals", "NO_EDGE",
          tf={"4h": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112),
              "1h": _pa(structure="LH_LL", ema20=90, ema50=100, ema200=110, last_close=88),
              "15m": _pa()})

    check("12_missing_orderflow_degraded_mode", "NO_EDGE",
          tf={"4h": _pa(), "1h": _pa(), "15m": _pa()},
          volume=_vol(False), futures=_fut(False), liquidations=_liq(False),
          data_quality={"data_quality_score": 65, "is_tradeable": True, "missing_fields": ["derivatives"], "stale_fields": []})

    # 13. Hysteresis: a single opposing candidate must NOT immediately flip
    #     an already-confirmed regime (non-override regimes only).
    state = {"confirmed_regime": "RANGE", "confirmed_at": _NOW.isoformat(), "pending_regime": None, "pending_count": 0}
    check("13_hysteresis_holds_on_first_candidate", "RANGE",
          tf={"4h": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112),
              "1h": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112),
              "15m": _pa(structure="HH_HL", ema20=110, ema50=100, ema200=90, last_close=112)},
          volume=_vol(True, cvd_slope_15m=Decimal("50")),
          futures=_fut(True, price_to_oi_relation="PRICE_UP_OI_UP", oi_change_1h_pct=Decimal("2")),
          previous_state=state)

    # 14. ...but a panic-liquidation candidate DOES override immediately.
    check("14_panic_liquidation_overrides_hysteresis_immediately", "PANIC_LIQUIDATION_LONGS",
          tf={"4h": _pa(atr_pct=Decimal("4")), "1h": _pa(atr_pct=Decimal("4")),
              "15m": _pa(atr_pct=Decimal("4"), pct_change=Decimal("-3.5"))},
          liquidations=_liq(True, cascade_detected=True, imbalance_15m=Decimal("-0.7")),
          previous_state=state)

    return results


try:
    report["synthetic_tests"] = run_synthetic_tests()
except Exception:
    report["synthetic_tests"] = {"_error": traceback.format_exc()}

synth_failed = [k for k, v in report["synthetic_tests"].items() if isinstance(v, dict) and not v.get("pass", False)]


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: live snapshot across all tracked symbols
# ══════════════════════════════════════════════════════════════════════════

async def run_live_snapshot() -> dict:
    settings = get_trading_settings()
    symbols = list(settings.symbols)
    ordered = (["BTCUSDT"] if "BTCUSDT" in symbols else []) + [s for s in symbols if s != "BTCUSDT"]

    results = {}
    btc_regime = None
    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await rs.classify_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                results[sym] = r
                if sym == "BTCUSDT":
                    btc_regime = r["primary_regime"]
            except Exception:
                results[sym] = {"error": traceback.format_exc()}

    valid = {s: r for s, r in results.items() if "error" not in r}
    regime_counts = Counter(r["primary_regime"] for r in valid.values())
    confidences = [r["confidence"] for r in valid.values()]
    dq_by_symbol = {s: r["data_quality"] for s, r in valid.items()}

    same_regime_warning = None
    if valid:
        top_regime, top_count = regime_counts.most_common(1)[0]
        if top_count / len(valid) > 0.8 and len(valid) > 3:
            same_regime_warning = (
                f"{top_count}/{len(valid)} symbols all classified as {top_regime} -- "
                f"plausible in a strong BTC-led market move, but check this isn't the "
                f"classifier just parroting BTC correlation rather than assessing each "
                f"symbol independently."
            )

    return {
        "symbols": results,
        "regime_distribution": dict(regime_counts),
        "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "num_no_edge": regime_counts.get("NO_EDGE", 0),
        "num_unstable_data": regime_counts.get("UNSTABLE_DATA", 0),
        "highest_data_quality": max(dq_by_symbol.items(), key=lambda kv: kv[1]) if dq_by_symbol else None,
        "lowest_data_quality": min(dq_by_symbol.items(), key=lambda kv: kv[1]) if dq_by_symbol else None,
        "same_regime_correlation_warning": same_regime_warning,
    }


# ══════════════════════════════════════════════════════════════════════════
# PHASE 3: historical replay (look-ahead guarded)
# ══════════════════════════════════════════════════════════════════════════

HIST_MAX_AGES = {"candles": 1e12, "trades": 1e12, "orderbook": 1e12, "derivatives": 1e12}


async def _fetch_all(session, symbol: str) -> dict:
    return {
        "c4h": [rs.candle_dict(c) for c in await qr.get_recent_candles(session, symbol, "240", limit=3000)],
        "c1h": [rs.candle_dict(c) for c in await qr.get_recent_candles(session, symbol, "60", limit=3000)],
        "c15m": [rs.candle_dict(c) for c in await qr.get_recent_candles(session, symbol, "15", limit=3000)],
        "trades": [rs.trade_agg_dict(r) for r in await qr.get_recent_trade_aggregates(session, symbol, 60, limit=5000)],
        # The collector only writes 1s/1m/5m liquidation buckets (see
        # aggregation.BUCKET_SECONDS) -- there is no 15m bucket in the DB.
        # compute_liquidation_features's 1m/5m/15m "windows" are built by
        # slicing trailing 1/5/15 rows out of this single 1-minute series
        # at each replay step, not by querying a bucket size that doesn't
        # exist (bucket_seconds=900 would silently return nothing and
        # disable panic-liquidation detection for the whole replay).
        "liq_1m_series": [rs.liq_agg_dict(r) for r in await qr.get_recent_liquidation_aggregates(session, symbol, 60, limit=5000)],
        "deriv": [rs.derivatives_dict(r) for r in await qr.get_recent_derivatives_snapshots(session, symbol, limit=3000)],
        "ob": [rs.orderbook_dict(r) for r in await qr.get_recent_orderbook_snapshots(session, symbol, limit=3000)],
    }


def _replay_symbol(symbol: str, data: dict, btc_regime_by_ts: dict | None) -> tuple[dict, dict]:
    """Returns (summary, regime_by_ts). LOOK-AHEAD GUARD: every window
    passed into feature_engine/classify is sliced to timestamps <= cutoff
    -- nothing after `cutoff` is visible at that step."""
    c4h = data["c4h"]
    if len(c4h) < 25:
        return {"symbol": symbol, "points": 0, "note": "insufficient 4h history for a meaningful replay"}, {}

    timeline = []
    regime_by_ts: dict = {}
    state = None
    for i in range(20, len(c4h)):
        cutoff = c4h[i]["open_time"]
        w4h = c4h[max(0, i - 100):i + 1]
        w1h = [c for c in data["c1h"] if c["open_time"] <= cutoff][-100:]
        w15m = [c for c in data["c15m"] if c["open_time"] <= cutoff][-100:]
        wtr = [r for r in data["trades"] if r["bucket_start"] <= cutoff][-30:]
        wliq_series = [r for r in data["liq_1m_series"] if r["bucket_start"] <= cutoff]
        wliq_1m = wliq_series[-1:]
        wliq_5m = wliq_series[-5:]
        wliq_15m = wliq_series[-15:]
        wderiv = [r for r in data["deriv"] if r["source_timestamp"] <= cutoff][-20:]
        wob = [r for r in data["ob"] if r["source_timestamp"] <= cutoff][-20:]

        built = rs.build_features_from_windows(
            w4h, w1h, w15m, wtr, wliq_1m, wliq_5m, wliq_15m, wob, wderiv, now=cutoff, max_ages=HIST_MAX_AGES,
        )
        btc_regime = btc_regime_by_ts.get(cutoff) if (btc_regime_by_ts and symbol != "BTCUSDT") else None
        result = rc.classify_from_features(
            symbol, cutoff, built["tf"], built["volume"], built["orderbook"], built["futures"],
            built["liquidations"], built["data_quality"], btc_regime=btc_regime, previous_state=state,
        )
        state = result.pop("_state", None)
        timeline.append({"timestamp": cutoff.isoformat(), "regime": result["primary_regime"], "confidence": result["confidence"]})
        regime_by_ts[cutoff] = result["primary_regime"]

    return _summarize_timeline(symbol, timeline, c4h), regime_by_ts


def _summarize_timeline(symbol: str, timeline: list, c4h: list) -> dict:
    if not timeline:
        return {"symbol": symbol, "points": 0}
    regimes = [t["regime"] for t in timeline]
    n = len(regimes)
    changes = sum(1 for i in range(1, n) if regimes[i] != regimes[i - 1])
    durations, run = [], 1
    for i in range(1, n):
        if regimes[i] == regimes[i - 1]:
            run += 1
        else:
            durations.append(run)
            run = 1
    durations.append(run)

    close_by_ts = {c["open_time"]: float(c["close"]) for c in c4h}
    ts_list = [c["open_time"] for c in c4h]
    trend_hits, trend_total = 0, 0
    breakout_hits, breakout_total = 0, 0
    for t in timeline:
        ts = dt.datetime.fromisoformat(t["timestamp"])
        try:
            pos = ts_list.index(ts)
        except ValueError:
            continue
        if pos + 3 >= len(ts_list):
            continue
        now_close = close_by_ts[ts_list[pos]]
        future_close = close_by_ts[ts_list[pos + 3]]
        moved_up = future_close > now_close
        if t["regime"] in ("TREND_UP", "TREND_DOWN"):
            trend_total += 1
            if (t["regime"] == "TREND_UP" and moved_up) or (t["regime"] == "TREND_DOWN" and not moved_up):
                trend_hits += 1
        if t["regime"] in ("BREAKOUT_UP", "BREAKOUT_DOWN"):
            breakout_total += 1
            future_range = max(close_by_ts[ts] for ts in ts_list[pos:pos + 4]) - min(close_by_ts[ts] for ts in ts_list[pos:pos + 4])
            past_range = max(close_by_ts[ts] for ts in ts_list[max(0, pos - 4):pos + 1]) - min(close_by_ts[ts] for ts in ts_list[max(0, pos - 4):pos + 1])
            if past_range > 0 and future_range > past_range:
                breakout_hits += 1

    return {
        "symbol": symbol,
        "points": n,
        "regime_counts": dict(Counter(regimes)),
        "num_regime_changes": changes,
        "avg_regime_duration_bars_4h": round(sum(durations) / len(durations), 1) if durations else None,
        "pct_time_no_edge": round(100 * regimes.count("NO_EDGE") / n, 1),
        "pct_time_unstable_data": round(100 * regimes.count("UNSTABLE_DATA") / n, 1),
        "trend_forward_alignment_pct_3bars": round(100 * trend_hits / trend_total, 1) if trend_total else None,
        "trend_points_evaluated": trend_total,
        "breakout_range_expansion_pct": round(100 * breakout_hits / breakout_total, 1) if breakout_total else None,
        "breakout_points_evaluated": breakout_total,
    }


async def run_historical_backtest() -> dict:
    settings = get_trading_settings()
    symbols = list(settings.symbols)
    ordered = (["BTCUSDT"] if "BTCUSDT" in symbols else []) + [s for s in symbols if s != "BTCUSDT"]

    summaries = {}
    btc_regime_by_ts: dict = {}
    async with session_scope() as session:
        for sym in ordered:
            try:
                data = await _fetch_all(session, sym)
                summary, regime_by_ts = _replay_symbol(sym, data, btc_regime_by_ts if sym != "BTCUSDT" else None)
                summaries[sym] = summary
                if sym == "BTCUSDT":
                    btc_regime_by_ts = regime_by_ts
            except Exception:
                summaries[sym] = {"symbol": sym, "error": traceback.format_exc()}
    return summaries


async def _run_phases_2_and_3() -> None:
    # Both phases share the process-wide lazy-singleton async engine from
    # core/database/session.py. Running them under two separate top-level
    # asyncio.run() calls binds that singleton to the first call's event
    # loop, then crashes cross-loop on the second (RuntimeError: ... attached
    # to a different loop) -- one shared loop for both phases avoids that.
    try:
        report["live_snapshot"] = await run_live_snapshot()
    except Exception:
        report["live_snapshot"] = {"error": traceback.format_exc()}

    try:
        report["historical_backtest"] = await run_historical_backtest()
    except Exception:
        report["historical_backtest"] = {"error": traceback.format_exc()}


asyncio.run(_run_phases_2_and_3())


# ══════════════════════════════════════════════════════════════════════════
# Write report + print summary
# ══════════════════════════════════════════════════════════════════════════

REPORT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

print("=" * 70)
print("REGIME CLASSIFIER v2 -- AUDIT REPORT")
print("=" * 70)
n_pass = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict) and v.get("pass"))
n_total = sum(1 for v in report["synthetic_tests"].values() if isinstance(v, dict))
print(f"Synthetic tests: {n_pass}/{n_total} PASS")
if synth_failed:
    for name in synth_failed:
        t = report["synthetic_tests"][name]
        print(f"  FAILED: {name} -> expected {t['expected']}, got {t['got']} (confidence {t['confidence']})")
print("-" * 70)
print("Live snapshot:")
ls = report["live_snapshot"]
if "error" in ls:
    print(f"  ERROR: {ls['error'][-2000:]}")
else:
    for sym, r in ls["symbols"].items():
        if "error" in r:
            print(f"  {sym:10s} ERROR: {r['error'][-2000:]}")
            continue
        top_reasons = "; ".join(r["reasons"][:3])
        top_counter = r["counterarguments"][0] if r["counterarguments"] else "(none)"
        print(f"  {sym:10s} {r['primary_regime']:26s} flags={r['secondary_flags']} "
              f"conf={r['confidence']:.2f} dq={r['data_quality']:.2f}")
        print(f"             reasons: {top_reasons}")
        print(f"             counter: {top_counter}")
    print(f"  regime distribution: {ls['regime_distribution']}")
    print(f"  avg confidence: {ls['avg_confidence']}  NO_EDGE: {ls['num_no_edge']}  UNSTABLE_DATA: {ls['num_unstable_data']}")
    print(f"  highest data quality: {ls['highest_data_quality']}  lowest: {ls['lowest_data_quality']}")
    if ls["same_regime_correlation_warning"]:
        print(f"  WARNING: {ls['same_regime_correlation_warning']}")
print("-" * 70)
print("Historical backtest (look-ahead guarded, 4h decision points):")
for sym, s in report["historical_backtest"].items():
    if "error" in s:
        print(f"  {sym:10s} ERROR: {s['error'][-2000:]}")
        continue
    if s.get("points", 0) == 0:
        print(f"  {sym:10s} {s.get('note', 'no data')}")
        continue
    print(f"  {sym:10s} points={s['points']} changes={s['num_regime_changes']} "
          f"avg_dur={s['avg_regime_duration_bars_4h']}bars NO_EDGE%={s['pct_time_no_edge']} "
          f"UNSTABLE%={s['pct_time_unstable_data']} "
          f"trend_align%={s['trend_forward_alignment_pct_3bars']} (n={s['trend_points_evaluated']}) "
          f"breakout_expansion%={s['breakout_range_expansion_pct']} (n={s['breakout_points_evaluated']})")
print("-" * 70)
print(f"Full report written to: {REPORT_PATH}")
