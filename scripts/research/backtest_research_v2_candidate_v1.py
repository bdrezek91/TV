"""Evaluate the contract-repaired Research V2 candidate on existing cache.

Runs no network calls and mutates no DB/paper/exchange state. It keeps the
baseline immutable and evaluates the research-only candidate under 8/day,
12/day and 24/day scan clocks using the same common W5 execution adapter.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import statistics
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_funnel_v1 import ResearchFunnelDiagnostics
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS, TradeObservation, decision_times, summarize_observations
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, write_json_cache

UTC = dt.timezone.utc
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(24)),
}


def _dt(v: str) -> dt.datetime:
    out = dt.datetime.fromisoformat(v)
    if out.tzinfo is None:
        raise ValueError(f"timezone required: {v}")
    return out.astimezone(UTC)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _load_backfill(cache_root: Path) -> dict:
    return json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))


def _selected_successful(backfill: dict, symbols_arg: str | None) -> tuple[list[str], dict]:
    successful = {
        symbol: row for symbol, row in (backfill.get("results") or {}).items()
        if row.get("status") == "OK" and row.get("manifest", {}).get("cache_dir")
    }
    selected = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] if symbols_arg else list(successful)
    missing = [s for s in selected if s not in successful]
    if missing:
        raise ValueError(f"missing successful cache for: {missing}")
    return selected, successful


def _future_candles(candles: list[dict], timestamps: list[dt.datetime], cutoff: dt.datetime) -> list[dict]:
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _summary_by_setup(rows: list[TradeObservation]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for row in rows:
        grouped[row.setup_name].append(row)
    return {name: summarize_observations(items) for name, items in sorted(grouped.items())}


def _rr_for_setup(setup: dict) -> float | None:
    direction = setup.get("direction")
    if direction not in {"LONG", "SHORT"}:
        return None
    zone = setup.get("entry_zone") or {}
    try:
        low = Decimal(str(zone["low"]))
        high = Decimal(str(zone["high"]))
        stop = Decimal(str(setup["stop_loss"]))
        target = Decimal(str((setup.get("targets") or [])[0]))
    except Exception:
        return None
    worst = high if direction == "LONG" else low
    risk = abs(worst - stop)
    if risk <= 0:
        return None
    return float(abs(target - worst) / risk)


def _rr_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "gte_1_5": sum(v >= 1.5 for v in values),
        "lt_1_5": sum(v < 1.5 for v in values),
    }


def _run_schedule(name: str, hours: tuple[int, ...], requested: list[str], successful: dict, start: dt.datetime, end: dt.datetime) -> dict:
    context_symbols = list(requested)
    if "BTCUSDT" not in context_symbols and "BTCUSDT" in successful:
        context_symbols = ["BTCUSDT"] + context_symbols
    elif "BTCUSDT" in context_symbols:
        context_symbols = ["BTCUSDT"] + [s for s in context_symbols if s != "BTCUSDT"]

    bundles = {s: load_extended_bundle(Path(successful[s]["manifest"]["cache_dir"])) for s in context_symbols}
    indexes = {s: HistoricalWindowIndex(b.inputs) for s, b in bundles.items()}
    candles = {s: list(b.inputs.candles_1m) for s, b in bundles.items()}
    candle_times = {s: [c["open_time"] for c in candles[s]] for s in bundles}
    states = {s: None for s in context_symbols}
    funnel = ResearchFunnelDiagnostics()
    cutoffs = decision_times(start, end, hours=hours)

    observations: list[TradeObservation] = []
    by_policy: dict[str, list[TradeObservation]] = {
        "CURRENT_W4": [],
        "BALANCED_CONFIRMED_OR_WEAK": [],
        "STRICT_CONFIRMED_ONLY": [],
    }
    adjustment_counts = Counter()
    rr_values: dict[str, list[float]] = defaultdict(list)
    w1_dq_adjusted = 0

    for cutoff in cutoffs:
        btc_regime = None
        chains = {}
        for symbol in context_symbols:
            chain = build_research_v2_candidate_chain_indexed(
                symbol,
                indexes[symbol],
                cutoff,
                btc_regime=btc_regime,
                previous_state=states[symbol],
            )
            states[symbol] = chain.get("_state")
            chains[symbol] = chain
            if chain.get("w1_classification_data_quality", {}).get("comparison_confidence_adjustment"):
                w1_dq_adjusted += 1
            if symbol == "BTCUSDT":
                btc_regime = chain["regime"].get("primary_regime")

        for symbol in requested:
            chain = chains[symbol]
            funnel.record(chain)
            for setup_type, setup in (chain.get("setups") or {}).items():
                if setup.get("direction") not in {"LONG", "SHORT"}:
                    continue
                for adj in setup.get("comparison_candidate_adjustments", []) or []:
                    adjustment_counts[f"{setup_type}:{adj}"] += 1
                rr = _rr_for_setup(setup)
                if rr is not None:
                    rr_values[setup_type].append(rr)

            signals = neutral_signals_from_research(chain, approved_only=True)
            for signal in signals:
                future = _future_candles(candles[symbol], candle_times[symbol], cutoff)
                result = simulate_neutral_signal(signal, future)
                obs = result.observation
                observations.append(obs)
                by_policy["CURRENT_W4"].append(obs)
                status = signal.metadata.get("confirmation_status")
                if status in {"CONFIRMED", "WEAK_CONFIRMATION"}:
                    by_policy["BALANCED_CONFIRMED_OR_WEAK"].append(obs)
                if status == "CONFIRMED":
                    by_policy["STRICT_CONFIRMED_ONLY"].append(obs)

    return {
        "schedule": {
            "name": name,
            "local_hours_europe_warsaw": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(requested),
            "total_symbol_decision_points": len(cutoffs) * len(requested),
        },
        "funnel": funnel.to_dict(),
        "w1_confidence_debias_events_including_context": w1_dq_adjusted,
        "geometry_adjustments": dict(adjustment_counts.most_common()),
        "tp1_rr_from_w4_worst_entry_by_setup": {k: _rr_summary(v) for k, v in sorted(rr_values.items())},
        "outcomes_current_w4": summarize_observations(observations),
        "outcomes_by_setup": _summary_by_setup(observations),
        "confirmation_policy_sensitivity": {k: summarize_observations(v) for k, v in by_policy.items()},
    }


def main(args) -> dict:
    cache_root = Path(args.cache_root)
    backfill = _load_backfill(cache_root)
    requested, successful = _selected_successful(backfill, args.symbols)
    window = backfill.get("window") or {}
    start = _dt(window["evaluation_start"])
    end = _dt(window["evaluation_end"]) - MIN_FUTURE_EXECUTION
    if args.start:
        start = max(start, _dt(args.start))
    if args.end:
        end = min(end, _dt(args.end))

    schedules = SCHEDULES
    if args.schedule:
        wanted = args.schedule.upper()
        if wanted not in schedules:
            raise ValueError(f"unknown schedule {wanted}; choose one of {sorted(schedules)}")
        schedules = {wanted: schedules[wanted]}

    results = {
        name: _run_schedule(name, hours, requested, successful, start, end)
        for name, hours in schedules.items()
    }
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": "RESEARCH_V2_CONTRACT_REPAIRED_V1",
        "status": "RESEARCH_ONLY_NOT_PROMOTED",
        "read_only": True,
        "evaluation_window": {"start": start, "end": end},
        "requested_symbols": requested,
        "results": results,
        "guards": [
            "baseline Research V2 is immutable",
            "no historical orderbook/liquidation values are fabricated",
            "W3 thresholds are unchanged",
            "all geometry repairs use only information available at decision time",
            "more trades are not evidence of better edge; compare W5 outcomes and sample size",
        ],
    }
    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / "research_v2_candidate_replay.json", payload)
    return payload


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    p.add_argument("--symbols", help="comma-separated subset of successful backfill symbols")
    p.add_argument("--start", help="optional timezone-aware ISO clamp")
    p.add_argument("--end", help="optional timezone-aware ISO clamp")
    p.add_argument("--schedule", choices=sorted(SCHEDULES), help="run only one schedule; default runs all three")
    return p.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
