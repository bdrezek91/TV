#!/usr/bin/env python3
"""Candidate V5: Failed Breakout / Fakeout Reversal comparison.

Read-only 30d research runner using the same extended cache, W1 candidate chain,
W3/W4 rules, lifecycle gate and W5 execution model as prior candidates.

Variants:
- MEAN_REVERSION_ONLY
- FAKEOUT_ONLY
- MEAN_REVERSION_PLUS_FAKEOUT

The fakeout detector is price-only at setup-generation time and uses only
closed candles available at the decision cutoff. Historical orderbook and
liquidations are not fabricated.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_fakeout_v1 import evaluate_failed_breakout
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
VARIANTS = {
    "MEAN_REVERSION_ONLY": {"mean_reversion"},
    "FAKEOUT_ONLY": {"failed_breakout"},
    "MEAN_REVERSION_PLUS_FAKEOUT": {"mean_reversion", "failed_breakout"},
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
    selected = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] if symbols_arg else list(successful)
    missing = [s for s in selected if s not in successful]
    if missing:
        raise ValueError(f"missing successful cache for: {missing}")
    return selected, successful


def _future_candles(candles: list[dict], timestamps: list[dt.datetime], cutoff: dt.datetime) -> list[dict]:
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _by_setup(rows: list[TradeObservation]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for row in rows:
        grouped[row.setup_name].append(row)
    return {name: summarize_observations(items) for name, items in sorted(grouped.items())}


def _lifecycle_variant(events: list[dict[str, Any]], allowed: set[str], end: dt.datetime) -> dict[str, Any]:
    # Same-symbol/same-scan cross-setup conflicts are excluded in the combined
    # variant rather than resolved using an arbitrary setup priority.
    eligible = [event for event in events if event["setup"] in allowed]
    conflict_keys: set[tuple[str, dt.datetime]] = set()
    if len(allowed) > 1:
        grouped: dict[tuple[str, dt.datetime], set[str]] = defaultdict(set)
        for event in eligible:
            grouped[(event["symbol"], event["decision_time"])].add(event["setup"])
        conflict_keys = {key for key, setups in grouped.items() if len(setups) > 1}
        eligible = [e for e in eligible if (e["symbol"], e["decision_time"]) not in conflict_keys]

    gate = SingleSymbolLifecycleGate()
    submitted: list[dict[str, Any]] = []
    suppressed = Counter()
    for event in eligible:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            suppressed[event["setup"]] += 1
            continue
        submitted.append(event)
        gate.occupy_from_order(event["symbol"], event["order"], fallback_end=end)

    rows = [event["observation"] for event in submitted]
    return {
        "lifecycle": summarize_observations(rows),
        "by_setup": _by_setup(rows),
        "submitted_by_setup": dict(Counter(event["setup"] for event in submitted)),
        "suppressed_busy_by_setup": dict(suppressed),
        "same_scan_cross_setup_conflicts_excluded": len(conflict_keys),
        "gate": gate.diagnostics(),
    }


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    requested: list[str],
    successful: dict,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
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

    events: list[dict[str, Any]] = []
    fakeout_funnel = Counter()
    fakeout_w3 = Counter()
    fakeout_w4 = Counter()
    fakeout_by_regime = Counter()

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
            future = _future_candles(candles[symbol], candle_times[symbol], cutoff)

            for signal in neutral_signals_from_research(chain, approved_only=True):
                if signal.setup_name != "mean_reversion":
                    continue
                result = simulate_neutral_signal(signal, future)
                events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "setup": "mean_reversion",
                    "signal": signal,
                    "observation": result.observation,
                    "order": result.order,
                })

            fake = evaluate_failed_breakout(chain, btc_regime=btc_regime)
            setup = fake["setup"]
            direction = setup.get("direction")
            if direction in {"LONG", "SHORT"}:
                fakeout_funnel["active_setup"] += 1
                fakeout_by_regime[str(chain["regime"].get("primary_regime"))] += 1
                confirmation = fake.get("confirmation") or {}
                risk = fake.get("risk") or {}
                fakeout_w3[str(confirmation.get("status"))] += 1
                fakeout_w4[str(risk.get("decision"))] += 1
            else:
                fakeout_funnel["no_setup"] += 1

            signal = fake.get("signal")
            if signal is not None:
                fakeout_funnel["approved_signal"] += 1
                result = simulate_neutral_signal(signal, future)
                events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "setup": "failed_breakout",
                    "signal": signal,
                    "observation": result.observation,
                    "order": result.order,
                })

    variants = {
        name: _lifecycle_variant(events, allowed, end)
        for name, allowed in VARIANTS.items()
    }
    return {
        "schedule": {
            "name": schedule_name,
            "hours": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(requested),
            "total_symbol_decision_points": len(cutoffs) * len(requested),
        },
        "event_pool_by_setup": dict(Counter(event["setup"] for event in events)),
        "fakeout_funnel": dict(fakeout_funnel),
        "fakeout_active_by_w1_regime": dict(fakeout_by_regime),
        "fakeout_w3": dict(fakeout_w3),
        "fakeout_w4": dict(fakeout_w4),
        "variants": variants,
        "interpretation_contract": (
            "Fakeout generation uses only decision-time closed candles. W3/W4/W5 are unchanged. "
            "Combined same-symbol/same-scan cross-setup conflicts are excluded instead of prioritized."
        ),
    }


def main(args) -> dict[str, Any]:
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

    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": "RESEARCH_V2_CANDIDATE_V5_FAILED_BREAKOUT_FAKEOUT",
        "status": "RESEARCH_ONLY_NOT_PROMOTED",
        "read_only": True,
        "evaluation_window": {"start": start, "end": end},
        "requested_symbols": requested,
        "fakeout_definition": {
            "reference_range_hours": 20,
            "sweep_lookback_15m_candles": 4,
            "sweep_min_atr": "0.10",
            "max_close_back_inside_distance_atr": "0.75",
            "entry_near_reclaimed_level": True,
            "targets_r": ["1.5", "2.5", "4.0"],
        },
        "results": {
            name: _run_schedule(name, hours, requested, successful, start, end)
            for name, hours in schedules.items()
        },
        "guards": [
            "production Research V2 unchanged",
            "mean_reversion logic unchanged",
            "fakeout has a fixed predeclared price-only detection rule",
            "no future candle used for setup generation",
            "W3/W4 thresholds unchanged",
            "W5 fees/slippage/funding unchanged",
            "no historical orderbook/liquidation values fabricated",
            "same-symbol same-scan cross-setup conflicts are excluded in combined variant",
        ],
    }
    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / "research_v2_candidate_v5_fakeout.json", payload)
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
