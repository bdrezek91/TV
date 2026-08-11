#!/usr/bin/env python3
"""Frozen 90-day non-overlapping holdout for Mean Reversion.

This runner is intentionally narrow:
- exact evaluation window: 2026-04-12 through 2026-07-10 UTC;
- exact five-symbol universe used in discovery;
- frozen Research V2 candidate chain and W5 execution semantics;
- only mean_reversion is eligible for lifecycle submission;
- no tuning variants, no hours/symbol/W3 filters, no new strategies.

The surrounding workflow must validate the dedicated holdout cache before this
runner is used.  This script additionally fails closed if the backfill window,
universe or cache root differs from the pre-registered contract.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_holdout_v1 import (
    FROZEN_SOURCE_BLOBS,
    HOLDOUT_DECISION_END,
    HOLDOUT_EVALUATION_END,
    HOLDOUT_EVALUATION_START,
    PASS_RULE,
    REQUIRED_SYMBOLS,
    assess_holdout,
    validate_holdout_backfill,
)
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS, TradeObservation, decision_times, summarize_observations
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import write_json_cache

UTC = dt.timezone.utc
SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(0, 24)),
}
DEFAULT_HOLDOUT_CACHE_ROOT = Path("/app/artifacts/research/backtest_comparison/holdout_90d/cache")
DEFAULT_OUTPUT = Path("/app/artifacts/research/backtest_comparison/holdout_90d/research_v2_mean_reversion_holdout_v1.json")


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


def _future_candles(candles: list[dict], timestamps: list[dt.datetime], cutoff: dt.datetime) -> list[dict]:
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _group_summary(events: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(key_fn(event))].append(event)
    return {
        key: {"metrics": summarize_observations([item["observation"] for item in items])}
        for key, items in sorted(grouped.items())
    }


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    successful: dict[str, Any],
) -> dict[str, Any]:
    requested = list(REQUIRED_SYMBOLS)
    bundles = {s: load_extended_bundle(Path(successful[s]["manifest"]["cache_dir"])) for s in requested}
    indexes = {s: HistoricalWindowIndex(bundle.inputs) for s, bundle in bundles.items()}
    candles = {s: list(bundle.inputs.candles_1m) for s, bundle in bundles.items()}
    candle_times = {s: [c["open_time"] for c in candles[s]] for s in bundles}
    states = {s: None for s in requested}
    cutoffs = decision_times(HOLDOUT_EVALUATION_START, HOLDOUT_DECISION_END, hours=hours)

    approved_events: list[dict[str, Any]] = []
    for cutoff in cutoffs:
        btc_regime = None
        chains: dict[str, dict[str, Any]] = {}
        for symbol in requested:
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
                approved_events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "direction": signal.direction,
                    "regime": signal.regime or "UNKNOWN",
                    "confirmation_status": str(signal.metadata.get("confirmation_status") or "UNKNOWN"),
                    "observation": result.observation,
                    "order": result.order,
                })

    gate = SingleSymbolLifecycleGate()
    lifecycle_events: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for event in approved_events:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            suppressed.append(event)
            continue
        lifecycle_events.append(event)
        gate.occupy_from_order(event["symbol"], event["order"], fallback_end=HOLDOUT_DECISION_END)

    suppressed_by_symbol: dict[str, int] = defaultdict(int)
    for event in suppressed:
        suppressed_by_symbol[event["symbol"]] += 1

    rows = [event["observation"] for event in lifecycle_events]
    return {
        "schedule": {
            "name": schedule_name,
            "hours": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(requested),
            "total_symbol_decision_points": len(cutoffs) * len(requested),
        },
        "approved_mean_reversion_event_pool": len(approved_events),
        "lifecycle_mean_reversion": summarize_observations(rows),
        "lifecycle_submitted": len(lifecycle_events),
        "lifecycle_suppressed_busy": len(suppressed),
        "lifecycle_suppressed_busy_by_symbol": dict(sorted(suppressed_by_symbol.items())),
        "groups": {
            "by_symbol": _group_summary(lifecycle_events, lambda e: e["symbol"]),
            "by_direction": _group_summary(lifecycle_events, lambda e: e["direction"]),
            "by_w1_regime": _group_summary(lifecycle_events, lambda e: e["regime"]),
            "by_w3_status": _group_summary(lifecycle_events, lambda e: e["confirmation_status"]),
        },
        "lifecycle_gate_diagnostics": gate.diagnostics(),
        "interpretation_contract": (
            "These groups are robustness diagnostics only. The frozen holdout decision is made by the pre-registered aggregate rule; "
            "hours, symbols, W3 statuses and regimes may not be filtered or retuned after observing this holdout."
        ),
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = Path(args.cache_root)
    summary_path = cache_root / "backfill_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    guard = validate_holdout_backfill(summary, cache_root)
    if not guard["passed"]:
        raise ValueError("holdout backfill contract failed: " + "; ".join(guard["errors"]))

    successful = {
        symbol: summary["results"][symbol]
        for symbol in REQUIRED_SYMBOLS
    }
    results = {
        name: _run_schedule(name, hours, successful)
        for name, hours in SCHEDULES.items()
    }
    assessment = assess_holdout(results)

    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": "RESEARCH_V2_MEAN_REVERSION_HOLDOUT_V1",
        "status": "FROZEN_OUT_OF_SAMPLE_VALIDATION",
        "read_only": True,
        "setup_scope": ["mean_reversion"],
        "candidate_geometry": "ALIGNED_TARGETS",
        "evaluation_window": {
            "start": HOLDOUT_EVALUATION_START,
            "raw_end": HOLDOUT_EVALUATION_END,
            "decision_end": HOLDOUT_DECISION_END,
        },
        "requested_symbols": list(REQUIRED_SYMBOLS),
        "frozen_source_blobs": FROZEN_SOURCE_BLOBS,
        "pre_registered_pass_rule": PASS_RULE,
        "backfill_contract": guard,
        "results": results,
        "holdout_assessment": assessment,
        "guards": [
            "holdout ends before discovery starts; no date overlap",
            "dedicated cache root is required and discovery cache is not reused",
            "exact same five-symbol universe as discovery",
            "frozen Research V2 candidate geometry and Mean Reversion setup only",
            "W1/W2/W3/W4 thresholds unchanged",
            "W5 execution, fees, slippage and funding assumptions unchanged",
            "same lifecycle gate as discovery",
            "no new strategy variants or threshold search on holdout",
            "FULL_24H_1H versus 2H remains sensitivity because W1 hysteresis is tick-based",
            "a HOLDOUT_FAIL must not be rescued by filtering symbols/hours/W3/regimes on this period",
        ],
    }
    output = Path(args.output)
    write_json_cache(output, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_HOLDOUT_CACHE_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
