#!/usr/bin/env python3
"""Frozen 120-day untouched historical OOS validation for Mean Reversion V2.

This runner must only be used with the dedicated pre-registered cache:
2025-12-13..2026-04-11 UTC evaluation + 10 warm-up days.

MR V2 itself was derived after later periods had already been observed, so this
is historical OOS rather than a forward test.  It remains untouched validation
because this exact earlier period was not inspected during V2 development.

No symbol/hour/direction/W3/cost filters are allowed.  The only eligible setup
is MR_V2_EXHAUSTION_RANGE_QUALITY with the frozen dust-aware research lifecycle.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import gc
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
from tradingview_mcp.core.backtest.comparison_mean_reversion_v2 import (
    MR_V2_NAME,
    evaluate_mean_reversion_v2,
)
from tradingview_mcp.core.backtest.comparison_mean_reversion_v2_holdout import (
    DEFAULT_MR_V2_HOLDOUT_CACHE_ROOT,
    DEFAULT_MR_V2_HOLDOUT_OUTPUT,
    FROZEN_V2_SOURCE_BLOBS,
    FROZEN_V2_SPEC,
    MR_V2_HOLDOUT_DECISION_END,
    MR_V2_HOLDOUT_EVALUATION_END,
    MR_V2_HOLDOUT_EVALUATION_START,
    PASS_RULE,
    assess_mr_v2_holdout,
    validate_mr_v2_holdout_backfill,
)
from tradingview_mcp.core.backtest.comparison_v1 import (
    DEFAULT_SCAN_HOURS,
    TradeObservation,
    decision_times,
    summarize_observations,
)
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import write_json_cache

SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(0, 24)),
}
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
    times: Sequence[dt.datetime],
    cutoff: dt.datetime,
) -> Sequence[dict[str, Any]]:
    return candles[bisect.bisect_left(times, cutoff):]


def _group_summary(
    events: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(key_fn(event))].append(event)
    return {
        key: {"metrics": summarize_observations([item["observation"] for item in items])}
        for key, items in sorted(grouped.items())
    }


def _collect_symbol_events(
    *,
    symbol: str,
    successful_row: dict[str, Any],
    cutoffs: Sequence[dt.datetime],
    btc_regimes: Sequence[str | None] | None,
) -> tuple[list[dict[str, Any]], list[str | None] | None]:
    is_btc = symbol == "BTCUSDT"
    if is_btc and btc_regimes is not None:
        raise ValueError("BTC must reconstruct its own regime context")
    if not is_btc and (btc_regimes is None or len(btc_regimes) != len(cutoffs)):
        raise ValueError(f"{symbol} requires one BTC regime per cutoff")

    bundle = load_extended_bundle(Path(successful_row["manifest"]["cache_dir"]))
    index = HistoricalWindowIndex(bundle.inputs)
    candles: Sequence[dict[str, Any]] = bundle.inputs.candles_1m
    candle_times = [candle["open_time"] for candle in candles]
    state = None
    events: list[dict[str, Any]] = []
    derived_btc: list[str | None] | None = [] if is_btc else None

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
            derived_btc.append(chain["regime"].get("primary_regime"))  # type: ignore[union-attr]

        gate = evaluate_mean_reversion_v2(chain)
        if not gate["eligible"]:
            continue
        for signal in neutral_signals_from_research(chain, approved_only=True):
            if signal.setup_name != "mean_reversion":
                continue
            result = simulate_neutral_signal(
                signal,
                _future_candles(candles, candle_times, cutoff),
            )
            events.append({
                "symbol": symbol,
                "decision_time": signal.decision_time,
                "direction": signal.direction,
                "regime": signal.regime or "UNKNOWN",
                "confirmation_status": str(signal.metadata.get("confirmation_status") or "UNKNOWN"),
                "v2_gate": gate,
                "observation": result.observation,
                "order": result.order,
            })

    return events, derived_btc


def _restore_event_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda event: (event["decision_time"], SYMBOL_ORDER[event["symbol"]]),
    )


def _run_schedule(
    name: str,
    hours: tuple[int, ...],
    successful: dict[str, Any],
) -> dict[str, Any]:
    cutoffs = decision_times(
        MR_V2_HOLDOUT_EVALUATION_START,
        MR_V2_HOLDOUT_DECISION_END,
        hours=hours,
    )
    events: list[dict[str, Any]] = []

    btc_events, btc_regimes = _collect_symbol_events(
        symbol="BTCUSDT",
        successful_row=successful["BTCUSDT"],
        cutoffs=cutoffs,
        btc_regimes=None,
    )
    events.extend(btc_events)
    if btc_regimes is None or len(btc_regimes) != len(cutoffs):
        raise RuntimeError("failed to reconstruct BTC regime context for all cutoffs")
    gc.collect()

    for symbol in REQUIRED_SYMBOLS[1:]:
        symbol_events, _ = _collect_symbol_events(
            symbol=symbol,
            successful_row=successful[symbol],
            cutoffs=cutoffs,
            btc_regimes=btc_regimes,
        )
        events.extend(symbol_events)
        gc.collect()

    events = _restore_event_order(events)
    gate = SingleSymbolLifecycleGate(close_target_dust=True)
    submitted: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for event in events:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            suppressed.append(event)
            continue
        submitted.append(event)
        gate.occupy_from_order(
            event["symbol"],
            event["order"],
            fallback_end=MR_V2_HOLDOUT_DECISION_END,
        )

    rows: list[TradeObservation] = [event["observation"] for event in submitted]
    by_symbol = _group_summary(submitted, lambda event: event["symbol"])
    # Keep the five-symbol breadth assessment fail-closed but represent a true
    # zero-trade symbol explicitly rather than crashing the assessor.
    for symbol in REQUIRED_SYMBOLS:
        if symbol not in by_symbol:
            by_symbol[symbol] = {"metrics": summarize_observations([])}

    suppressed_by_symbol: Counter[str] = Counter(event["symbol"] for event in suppressed)
    return {
        "schedule": {
            "name": name,
            "hours": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(REQUIRED_SYMBOLS),
            "total_symbol_decision_points": len(cutoffs) * len(REQUIRED_SYMBOLS),
        },
        "mr_v2_eligible_event_pool": len(events),
        "lifecycle_mean_reversion": summarize_observations(rows),
        "lifecycle_submitted": len(submitted),
        "lifecycle_suppressed_busy": len(suppressed),
        "lifecycle_suppressed_busy_by_symbol": dict(sorted(suppressed_by_symbol.items())),
        "groups": {
            "by_symbol": dict(sorted(by_symbol.items())),
            "by_direction": _group_summary(submitted, lambda event: event["direction"]),
            "by_w3_status": _group_summary(submitted, lambda event: event["confirmation_status"]),
        },
        "lifecycle_gate_diagnostics": gate.diagnostics(),
        "runtime_execution_plan": {
            "bundle_loading": "SEQUENTIAL_ONE_SYMBOL_AT_A_TIME",
            "btc_context": "PRECOMPUTED_PER_CUTOFF_WITH_IDENTICAL_HYSTERESIS",
            "event_order": "RESTORED_CUTOFF_MAJOR_REQUIRED_SYMBOL_ORDER",
            "lifecycle_target_dust_repair": True,
            "strategy_or_threshold_change": False,
        },
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = Path(args.cache_root)
    summary = json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))
    contract = validate_mr_v2_holdout_backfill(summary, cache_root)
    if not contract["passed"]:
        raise ValueError("MR V2 holdout contract failed: " + "; ".join(contract["errors"]))

    successful = {symbol: summary["results"][symbol] for symbol in REQUIRED_SYMBOLS}
    results: dict[str, Any] = {}
    for name, hours in SCHEDULES.items():
        results[name] = _run_schedule(name, hours, successful)
        gc.collect()

    assessment = assess_mr_v2_holdout(results)
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": MR_V2_NAME,
        "status": "FROZEN_UNTOUCHED_HISTORICAL_OOS_VALIDATION",
        "read_only": True,
        "historical_oos_not_forward_test": True,
        "evaluation_window": {
            "start": MR_V2_HOLDOUT_EVALUATION_START,
            "raw_end": MR_V2_HOLDOUT_EVALUATION_END,
            "decision_end": MR_V2_HOLDOUT_DECISION_END,
        },
        "requested_symbols": list(REQUIRED_SYMBOLS),
        "frozen_v2_spec": FROZEN_V2_SPEC,
        "frozen_source_blobs": FROZEN_V2_SOURCE_BLOBS,
        "pre_registered_pass_rule": PASS_RULE,
        "backfill_contract": contract,
        "results": results,
        "holdout_assessment": assessment,
        "guards": [
            "120d holdout was pre-registered before download/outcome inspection",
            "holdout ends before all MR V2 development data begins",
            "dedicated cache root; discovery and MR V1 holdout caches are not reused",
            "exact same BTC/ETH/SOL/XRP/BNB universe",
            "exact one MR V2 candidate; no symbol/hour/direction/W3/cost filtering",
            "existing MR V1 W1/W2/W3/W4 geometry remains unchanged",
            "same neutral W5 fees/slippage/funding assumptions",
            "dust-aware lifecycle is frozen as part of MR V2 specification",
            "memory-safe sequential bundle loading only changes resource usage",
            "FAIL must not be rescued by retuning on this 120d period",
            "PASS still requires subsequent shadow/paper forward validation",
        ],
    }
    output = Path(args.output)
    write_json_cache(output, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_MR_V2_HOLDOUT_CACHE_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_MR_V2_HOLDOUT_OUTPUT))
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
