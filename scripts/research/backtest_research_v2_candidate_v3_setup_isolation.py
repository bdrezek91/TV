#!/usr/bin/env python3
"""Research V2 Candidate V3: isolate setup contribution under identical inputs.

Research-only, read-only benchmark.  It keeps the ALIGNED_TARGETS decision chain,
current W3/W4 rules, W5 execution semantics, fees/slippage/funding assumptions and
historical degraded-data contract unchanged.  The only experimental variable is
which approved setup families are allowed to submit to the lifecycle simulator.

Variants:
- ALL_SETUPS: control (breakout + mean_reversion + trend_pullback)
- MEAN_REVERSION_ONLY
- MEAN_REVERSION_PLUS_BREAKOUT

Raw independent-scan outcomes and lifecycle-aware outcomes are reported separately.
No orderbook/liquidation values are fabricated.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_funnel_v1 import ResearchFunnelDiagnostics
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS, TradeObservation, decision_times, summarize_observations
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, write_json_cache

UTC = dt.timezone.utc
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(0, 24)),
}
SETUP_VARIANTS = {
    "ALL_SETUPS": frozenset({"breakout", "mean_reversion", "trend_pullback"}),
    "MEAN_REVERSION_ONLY": frozenset({"mean_reversion"}),
    "MEAN_REVERSION_PLUS_BREAKOUT": frozenset({"mean_reversion", "breakout"}),
}


def _dt(value: str) -> dt.datetime:
    out = dt.datetime.fromisoformat(value)
    if out.tzinfo is None:
        raise ValueError(f"timezone required: {value}")
    return out.astimezone(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _load_backfill(cache_root: Path) -> dict:
    return json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))


def _selected_successful(backfill: dict, symbols_arg: str | None) -> tuple[list[str], dict]:
    successful = {
        symbol: row
        for symbol, row in (backfill.get("results") or {}).items()
        if row.get("status") == "OK" and row.get("manifest", {}).get("cache_dir")
    }
    selected = (
        [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
        if symbols_arg
        else list(successful)
    )
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


def _num(summary: dict, key: str) -> float | None:
    value = summary.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _delta(candidate: dict, control: dict) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in ("signals", "filled", "trades_with_r", "win_rate", "expectancy_r", "profit_factor", "pnl_net", "fees", "slippage", "funding"):
        a = _num(candidate, key)
        b = _num(control, key)
        out[key] = None if a is None or b is None else a - b
    return out


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    requested: list[str],
    successful: dict,
    start: dt.datetime,
    end: dt.datetime,
) -> dict:
    context_symbols = list(requested)
    if "BTCUSDT" not in context_symbols and "BTCUSDT" in successful:
        context_symbols = ["BTCUSDT"] + context_symbols
    elif "BTCUSDT" in context_symbols:
        context_symbols = ["BTCUSDT"] + [s for s in context_symbols if s != "BTCUSDT"]

    bundles = {s: load_extended_bundle(Path(successful[s]["manifest"]["cache_dir"])) for s in context_symbols}
    indexes = {s: HistoricalWindowIndex(bundle.inputs) for s, bundle in bundles.items()}
    candles = {s: list(bundle.inputs.candles_1m) for s, bundle in bundles.items()}
    candle_times = {s: [c["open_time"] for c in candles[s]] for s in bundles}
    states = {s: None for s in context_symbols}
    cutoffs = decision_times(start, end, hours=hours)
    funnel = ResearchFunnelDiagnostics()

    # One immutable pool of approved decisions/executions.  Every V3 variant is
    # a subset of this exact pool, so setup inclusion is the only variable.
    events: list[dict[str, Any]] = []
    for cutoff in cutoffs:
        btc_regime = None
        chains: dict[str, dict] = {}
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
            if symbol == "BTCUSDT":
                btc_regime = chain["regime"].get("primary_regime")

        for symbol in requested:
            chain = chains[symbol]
            funnel.record(chain)
            for signal in neutral_signals_from_research(chain, approved_only=True):
                future = _future_candles(candles[symbol], candle_times[symbol], cutoff)
                result = simulate_neutral_signal(signal, future)
                events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "setup": signal.setup_name,
                    "observation": result.observation,
                    "order": result.order,
                    "confirmation_status": str(signal.metadata.get("confirmation_status")),
                })

    variants: dict[str, dict[str, Any]] = {}
    for variant_name, allowed_setups in SETUP_VARIANTS.items():
        raw_rows = [e["observation"] for e in events if e["setup"] in allowed_setups]
        gate = SingleSymbolLifecycleGate()
        lifecycle_rows: list[TradeObservation] = []
        suppressed_busy_by_setup: dict[str, int] = defaultdict(int)
        submitted_by_setup: dict[str, int] = defaultdict(int)

        for event in events:
            if event["setup"] not in allowed_setups:
                continue
            symbol = event["symbol"]
            decision_time = event["decision_time"]
            if not gate.can_submit(symbol, decision_time):
                suppressed_busy_by_setup[event["setup"]] += 1
                continue
            lifecycle_rows.append(event["observation"])
            submitted_by_setup[event["setup"]] += 1
            gate.occupy_from_order(symbol, event["order"], fallback_end=end)

        variants[variant_name] = {
            "allowed_setups": sorted(allowed_setups),
            "raw_independent_scan_outcomes": summarize_observations(raw_rows),
            "raw_independent_scan_outcomes_by_setup": _summary_by_setup(raw_rows),
            "lifecycle_current_w4": summarize_observations(lifecycle_rows),
            "lifecycle_current_w4_by_setup": _summary_by_setup(lifecycle_rows),
            "lifecycle_submitted_by_setup": dict(sorted(submitted_by_setup.items())),
            "lifecycle_suppressed_busy_by_setup": dict(sorted(suppressed_busy_by_setup.items())),
            "lifecycle_gate_diagnostics": gate.diagnostics(),
        }

    control = variants["ALL_SETUPS"]
    for variant_name, row in variants.items():
        row["delta_vs_all_setups"] = {
            "raw": _delta(row["raw_independent_scan_outcomes"], control["raw_independent_scan_outcomes"]),
            "lifecycle": _delta(row["lifecycle_current_w4"], control["lifecycle_current_w4"]),
        }

    return {
        "schedule": {
            "name": schedule_name,
            "local_hours_europe_warsaw": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(requested),
            "total_symbol_decision_points": len(cutoffs) * len(requested),
        },
        "shared_funnel": funnel.to_dict(),
        "approved_event_pool": len(events),
        "variants": variants,
        "interpretation_contract": (
            "All variants use the same ALIGNED_TARGETS W1-W4 decision pool and identical W5 simulation. "
            "Only setup-family eligibility changes; lifecycle occupancy is recomputed after filtering."
        ),
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
        schedules = {wanted: SCHEDULES[wanted]}

    results = {
        schedule_name: _run_schedule(schedule_name, hours, requested, successful, start, end)
        for schedule_name, hours in schedules.items()
    }
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": "RESEARCH_V2_CANDIDATE_V3_SETUP_ISOLATION",
        "status": "RESEARCH_ONLY_NOT_PROMOTED",
        "read_only": True,
        "candidate_geometry": "ALIGNED_TARGETS",
        "evaluation_window": {"start": start, "end": end},
        "requested_symbols": requested,
        "results": results,
        "guards": [
            "production Research V2 is unchanged",
            "ALIGNED_TARGETS geometry is fixed across all V3 variants",
            "W1/W2/W3/W4 thresholds are unchanged",
            "W5 execution, fees, slippage and funding assumptions are unchanged",
            "no historical orderbook/liquidation values are fabricated",
            "all variants are subsets of one identical approved event pool",
            "lifecycle occupancy is recomputed after setup filtering",
            "raw independent-scan counts remain separate from lifecycle-aware outcomes",
            "more trades are not evidence of better edge",
        ],
    }
    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / "research_v2_candidate_v3_setup_isolation.json", payload)
    return payload


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--schedule", choices=sorted(SCHEDULES))
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
