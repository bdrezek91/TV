"""Development-only replay for the V14 breakout hypothesis.

The runner refuses the reserved 120-day holdout cache by path.  It is intended
only for already-observed development datasets and does not promote V14.
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

from tradingview_mcp.core.backtest.comparison_breakout_v14 import (
    V14_NAME,
    evaluate_breakout_v14,
    validate_v14_development_cache_root,
)
from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
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
SYMBOL_ORDER = {symbol: index for index, symbol in enumerate(REQUIRED_SYMBOLS)}
DEFAULT_OUTPUT_NAME = "research_v14_breakout_development.json"


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
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


def _future(candles: Sequence[dict[str, Any]], timestamps: Sequence[dt.datetime], cutoff: dt.datetime):
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _group(events: list[dict[str, Any]], key: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for event in events:
        grouped[str(key(event))].append(event["observation"])
    return {name: summarize_observations(rows) for name, rows in sorted(grouped.items())}


def _collect_symbol(
    symbol: str,
    successful_row: dict[str, Any],
    cutoffs: Sequence[dt.datetime],
    btc_regimes: Sequence[str | None] | None,
) -> tuple[list[dict[str, Any]], list[str | None] | None, Counter]:
    is_btc = symbol == "BTCUSDT"
    if not is_btc and (btc_regimes is None or len(btc_regimes) != len(cutoffs)):
        raise ValueError(f"{symbol} requires BTC context for every cutoff")
    bundle = load_extended_bundle(Path(successful_row["manifest"]["cache_dir"]))
    index = HistoricalWindowIndex(bundle.inputs)
    candles = list(bundle.inputs.candles_1m)
    timestamps = [candle["open_time"] for candle in candles]
    state = None
    events: list[dict[str, Any]] = []
    derived_btc: list[str | None] | None = [] if is_btc else None
    funnel: Counter = Counter()
    for cutoff_index, cutoff in enumerate(cutoffs):
        btc_regime = None if is_btc else btc_regimes[cutoff_index]  # type: ignore[index]
        chain = build_research_v2_candidate_chain_indexed(
            symbol, index, cutoff, btc_regime=btc_regime, previous_state=state
        )
        state = chain.get("_state")
        if is_btc:
            derived_btc.append(chain["regime"].get("primary_regime"))  # type: ignore[union-attr]
        evaluated = evaluate_breakout_v14(chain, btc_regime=btc_regime)
        direction = evaluated["setup"].get("direction")
        if direction not in {"LONG", "SHORT"}:
            funnel["no_base_breakout"] += 1
            continue
        funnel["base_breakout"] += 1
        if not evaluated["gates"]["eligible"]:
            funnel["rejected_by_v14_gates"] += 1
            for reason in evaluated["gates"]["rejection_reasons"]:
                funnel[f"reject:{reason}"] += 1
            continue
        signal = evaluated.get("signal")
        if signal is None:
            funnel["rejected_by_existing_w3_w4"] += 1
            continue
        funnel["approved_v14_signal"] += 1
        result = simulate_neutral_signal(signal, _future(candles, timestamps, cutoff))
        events.append({
            "symbol": symbol,
            "decision_time": signal.decision_time,
            "direction": signal.direction,
            "regime": signal.regime or "UNKNOWN",
            "observation": result.observation,
            "order": result.order,
        })
    return events, derived_btc, funnel


def _run_schedule(name: str, hours: tuple[int, ...], successful: dict[str, Any], start: dt.datetime, end: dt.datetime):
    cutoffs = decision_times(start, end, hours=hours)
    events: list[dict[str, Any]] = []
    funnel: Counter = Counter()
    btc_events, btc_regimes, btc_funnel = _collect_symbol("BTCUSDT", successful["BTCUSDT"], cutoffs, None)
    events.extend(btc_events)
    funnel.update(btc_funnel)
    gc.collect()
    if btc_regimes is None or len(btc_regimes) != len(cutoffs):
        raise RuntimeError("failed to reconstruct BTC context")
    for symbol in REQUIRED_SYMBOLS[1:]:
        symbol_events, _, symbol_funnel = _collect_symbol(symbol, successful[symbol], cutoffs, btc_regimes)
        events.extend(symbol_events)
        funnel.update(symbol_funnel)
        gc.collect()
    events.sort(key=lambda event: (event["decision_time"], SYMBOL_ORDER[event["symbol"]]))
    gate = SingleSymbolLifecycleGate()
    submitted: list[dict[str, Any]] = []
    for event in events:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            continue
        submitted.append(event)
        gate.occupy_from_order(event["symbol"], event["order"], fallback_end=end)
    return {
        "schedule": {"name": name, "hours": list(hours), "cutoffs_per_symbol": len(cutoffs)},
        "funnel": dict(funnel),
        "lifecycle": summarize_observations([event["observation"] for event in submitted]),
        "by_symbol": _group(submitted, lambda event: event["symbol"]),
        "by_direction": _group(submitted, lambda event: event["direction"]),
        "lifecycle_gate": gate.diagnostics(),
        "event_ledger": [
            {
                "symbol": event["symbol"],
                "decision_time": event["decision_time"],
                "direction": event["direction"],
                "regime": event["regime"],
                "filled": event["observation"].filled,
                "r_multiple": event["observation"].r_multiple,
            }
            for event in submitted
        ],
    }


def _cadence_overlap(results: dict[str, Any]) -> dict[str, Any]:
    one_h = results["FULL_24H_1H"]["event_ledger"]
    two_h = results["FULL_24H_2H"]["event_ledger"]
    key = lambda event: (event["symbol"], event["decision_time"], event["direction"])
    one_index = {key(event): event for event in one_h}
    two_index = {key(event): event for event in two_h}
    common = sorted(set(one_index) & set(two_index), key=lambda row: (row[1], SYMBOL_ORDER[row[0]], row[2]))
    only_one = set(one_index) - set(two_index)
    only_two = set(two_index) - set(one_index)
    return {
        "definition": "event identity is symbol + decision_time + direction",
        "full_1h_events": len(one_index),
        "full_2h_events": len(two_index),
        "common_events": len(common),
        "only_full_1h": len(only_one),
        "only_full_2h": len(only_two),
        "jaccard": (len(common) / len(set(one_index) | set(two_index))) if (one_index or two_index) else None,
        "common_r_mismatches": sum(
            one_index[row]["r_multiple"] != two_index[row]["r_multiple"] for row in common
        ),
        "interpretation": (
            "Low overlap at timestamps shared by both schedules indicates cadence-dependent "
            "W1 state/hysteresis, not merely extra opportunities from hourly scanning."
        ),
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = validate_v14_development_cache_root(Path(args.cache_root))
    summary = json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))
    successful = {
        symbol: (summary.get("results") or {}).get(symbol)
        for symbol in REQUIRED_SYMBOLS
        if ((summary.get("results") or {}).get(symbol) or {}).get("status") == "OK"
    }
    missing = [symbol for symbol in REQUIRED_SYMBOLS if symbol not in successful]
    if missing:
        raise ValueError(f"V14 development requires frozen five-symbol universe; missing {missing}")
    window = summary.get("window") or {}
    start = _dt(window["evaluation_start"])
    raw_end = _dt(window["evaluation_end"])
    end = raw_end - MIN_FUTURE_EXECUTION
    results = {name: _run_schedule(name, hours, successful, start, end) for name, hours in SCHEDULES.items()}
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": V14_NAME,
        "status": "POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION",
        "read_only": True,
        "reserved_120d_holdout_used": False,
        "evaluation_window": {"start": start, "raw_end": raw_end, "decision_end": end},
        "requested_symbols": list(REQUIRED_SYMBOLS),
        "results": results,
        "cadence_diagnostics": _cadence_overlap(results),
        "guards": [
            "reserved holdout_mrv2_120d path is rejected",
            "point-in-time candidate chain only",
            "one fixed V14 hypothesis; no symbol/hour/direction filtering",
            "sequential one-symbol loading restores cutoff-major event order",
            "development output cannot promote V14",
        ],
    }
    output = Path(args.output) if args.output else cache_root.parent / DEFAULT_OUTPUT_NAME
    write_json_cache(output, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--output")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
