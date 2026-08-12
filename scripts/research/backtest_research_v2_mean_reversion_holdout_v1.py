#!/usr/bin/env python3
"""Frozen 90-day non-overlapping holdout for Mean Reversion.

This runner is intentionally narrow:
- exact evaluation window: 2026-04-12 through 2026-07-10 UTC;
- exact five-symbol universe used in discovery;
- frozen Research V2 candidate chain and W5 execution semantics;
- only mean_reversion is eligible for lifecycle submission;
- no tuning variants, no hours/symbol/W3 filters, no new strategies.

The surrounding workflow must validate the dedicated holdout cache before this
runner is used. This script additionally fails closed if the backfill window,
universe or cache root differs from the pre-registered contract.

Runtime note: symbols are loaded sequentially, one extended bundle at a time.
BTC is evaluated first to reconstruct the exact per-cutoff BTC regime context;
other symbols are then evaluated independently with that frozen context. Events
are sorted back into the original cutoff-major / symbol-major order before the
lifecycle gate. This changes memory usage only, not signal or execution rules.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import gc
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

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
SYMBOL_ORDER = {symbol: index for index, symbol in enumerate(REQUIRED_SYMBOLS)}


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


def _future_candles(
    candles: Sequence[dict[str, Any]],
    timestamps: Sequence[dt.datetime],
    cutoff: dt.datetime,
) -> Sequence[dict[str, Any]]:
    """Return the same cutoff tail used by discovery.

    The slice is intentionally preserved rather than changing W5 input
    semantics during validation. Sequential bundle loading is the memory fix.
    """
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _group_summary(events: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(key_fn(event))].append(event)
    return {
        key: {"metrics": summarize_observations([item["observation"] for item in items])}
        for key, items in sorted(grouped.items())
    }


def _collect_symbol_events(
    symbol: str,
    successful_row: dict[str, Any],
    cutoffs: Sequence[dt.datetime],
    *,
    btc_regimes: Sequence[str | None] | None,
) -> tuple[list[dict[str, Any]], list[str | None] | None]:
    """Evaluate one symbol while only that symbol's 90d bundle is resident.

    For BTC, ``btc_regimes`` must be None and the exact primary regime at each
    cutoff is returned for use by all other symbols. Non-BTC symbols receive
    that precomputed context. Hysteresis state remains independent per symbol,
    exactly as in the original cutoff-major implementation.
    """
    is_btc = symbol == "BTCUSDT"
    if is_btc and btc_regimes is not None:
        raise ValueError("BTC must be evaluated without external btc_regimes")
    if not is_btc and (btc_regimes is None or len(btc_regimes) != len(cutoffs)):
        raise ValueError(f"{symbol} requires one BTC regime value per cutoff")

    cache_dir = Path(successful_row["manifest"]["cache_dir"])
    bundle = load_extended_bundle(cache_dir)
    index = HistoricalWindowIndex(bundle.inputs)
    candles: Sequence[dict[str, Any]] = bundle.inputs.candles_1m
    candle_times = [c["open_time"] for c in candles]

    state = None
    events: list[dict[str, Any]] = []
    derived_btc_regimes: list[str | None] | None = [] if is_btc else None

    for cutoff_index, cutoff in enumerate(cutoffs):
        btc_regime = None if is_btc else btc_regimes[cutoff_index]  # type: ignore[index]
        chain = build_research_v2_candidate_chain_indexed(
            symbol,
            index,
            cutoff,
            btc_regime=btc_regime,
            previous_state=state,
        )
        state = chain.get("_state")

        if is_btc:
            derived_btc_regimes.append(chain["regime"].get("primary_regime"))  # type: ignore[union-attr]

        for signal in neutral_signals_from_research(chain, approved_only=True):
            if signal.setup_name != "mean_reversion":
                continue
            future = _future_candles(candles, candle_times, cutoff)
            result = simulate_neutral_signal(signal, future)
            events.append({
                "symbol": symbol,
                "decision_time": signal.decision_time,
                "direction": signal.direction,
                "regime": signal.regime or "UNKNOWN",
                "confirmation_status": str(signal.metadata.get("confirmation_status") or "UNKNOWN"),
                "observation": result.observation,
                "order": result.order,
            })

    return events, derived_btc_regimes


def _restore_original_event_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore original cutoff-major then REQUIRED_SYMBOLS order deterministically."""
    return sorted(
        events,
        key=lambda event: (
            event["decision_time"],
            SYMBOL_ORDER[event["symbol"]],
        ),
    )


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    successful: dict[str, Any],
) -> dict[str, Any]:
    requested = list(REQUIRED_SYMBOLS)
    cutoffs = decision_times(HOLDOUT_EVALUATION_START, HOLDOUT_DECISION_END, hours=hours)

    # Memory-safe evaluation plan: at most one extended 90d bundle is loaded at
    # a time. BTC goes first solely to reproduce the exact btc_regime context
    # that the original implementation supplied to the remaining symbols.
    approved_events: list[dict[str, Any]] = []

    btc_events, btc_regimes = _collect_symbol_events(
        "BTCUSDT",
        successful["BTCUSDT"],
        cutoffs,
        btc_regimes=None,
    )
    approved_events.extend(btc_events)
    if btc_regimes is None or len(btc_regimes) != len(cutoffs):
        raise RuntimeError("failed to reconstruct BTC regime context for every cutoff")
    gc.collect()

    for symbol in requested[1:]:
        symbol_events, _ = _collect_symbol_events(
            symbol,
            successful[symbol],
            cutoffs,
            btc_regimes=btc_regimes,
        )
        approved_events.extend(symbol_events)
        # The symbol bundle/index frame has returned and can now be reclaimed
        # before the next 90d symbol is loaded.
        gc.collect()

    # Sequential loading changes discovery order in memory (all BTC events,
    # then all ETH, ...). Restore the original cutoff-major ordering before the
    # lifecycle gate and summary/bootstrap code to keep deterministic outputs.
    approved_events = _restore_original_event_order(approved_events)

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
        "runtime_execution_plan": {
            "bundle_loading": "SEQUENTIAL_ONE_SYMBOL_AT_A_TIME",
            "btc_context": "PRECOMPUTED_PER_CUTOFF_WITH_IDENTICAL_HYSTERESIS",
            "event_order": "RESTORED_CUTOFF_MAJOR_REQUIRED_SYMBOL_ORDER",
            "strategy_or_threshold_change": False,
        },
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
    results: dict[str, Any] = {}
    for name, hours in SCHEDULES.items():
        results[name] = _run_schedule(name, hours, successful)
        gc.collect()

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
        "runtime_execution_plan": {
            "bundle_loading": "SEQUENTIAL_ONE_SYMBOL_AT_A_TIME",
            "reason": "RESOURCE_SAFETY_ONLY_AFTER_4GB_VPS_HARD_FREEZE",
            "strategy_or_threshold_change": False,
        },
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
            "runtime memory optimization loads one symbol at a time and restores original event order",
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