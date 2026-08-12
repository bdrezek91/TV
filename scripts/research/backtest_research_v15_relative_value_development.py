"""Development-only runner for frozen V15 BTC-neutral residual reversion.

The runner reads already-observed caches, refuses the reserved MR V2 holdout,
and cannot promote V15.  Each trade contains synchronized alt and BTC legs.
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
from typing import Any, Callable, Mapping, Sequence

from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_relative_value_v15 import (
    ALT_SYMBOLS,
    MAX_HOLD_HOURS,
    V15_NAME,
    detect_v15_pair_signal,
    simulate_v15_pair,
    validate_v15_development_cache_root,
)
from tradingview_mcp.core.backtest.comparison_v1 import (
    DEFAULT_SCAN_HOURS,
    TradeObservation,
    decision_times,
    summarize_observations,
)
from tradingview_mcp.core.backtest.extended_history_loader_v1 import (
    load_extended_bundle,
)
from tradingview_mcp.core.backtest.extended_history_v1 import (
    DEFAULT_CACHE_ROOT,
    write_json_cache,
)

UTC = dt.timezone.utc
MIN_FUTURE_EXECUTION = dt.timedelta(hours=MAX_HOLD_HOURS)
SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(24)),
}
SYMBOL_ORDER = {symbol: index for index, symbol in enumerate(REQUIRED_SYMBOLS)}
DEFAULT_OUTPUT_NAME = "research_v15_relative_value_development.json"
DEVELOPMENT_PASS_RULE = {
    "positive_expectancy_schedules_min": 2,
    "profit_factor_threshold": Decimal("1.15"),
    "profit_factor_schedules_min": 2,
    "catastrophic_schedule_floor_expectancy_r": Decimal("-0.10"),
    "full_24h_1h_min_terminal_pairs": 100,
    "positive_95pct_ci_low_schedules_min": 1,
    "max_single_pair_share_of_positive_pnl": Decimal("0.60"),
    "common_r_mismatches_max": 0,
}


def _dt(value: Any) -> dt.datetime:
    result = (
        value
        if isinstance(value, dt.datetime)
        else dt.datetime.fromisoformat(str(value))
    )
    if result.tzinfo is None:
        raise ValueError(f"timezone required: {value}")
    return result.astimezone(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _future_slice(
    candles: Sequence[Mapping[str, Any]],
    timestamps: Sequence[dt.datetime],
    cutoff: dt.datetime,
) -> list[Mapping[str, Any]]:
    start = bisect.bisect_left(timestamps, cutoff)
    end_time = cutoff + MIN_FUTURE_EXECUTION + dt.timedelta(minutes=1)
    end = bisect.bisect_right(timestamps, end_time)
    return list(candles[start:end])


def _collect_pair(
    alt_symbol: str,
    alt_row: Mapping[str, Any],
    btc_bundle: Any,
    cutoffs: Sequence[dt.datetime],
) -> tuple[list[dict[str, Any]], Counter]:
    alt_bundle = load_extended_bundle(Path(alt_row["manifest"]["cache_dir"]))
    alt_1h = list(alt_bundle.inputs.candles_1h)
    btc_1h = list(btc_bundle.inputs.candles_1h)
    alt_1m = list(alt_bundle.inputs.candles_1m)
    btc_1m = list(btc_bundle.inputs.candles_1m)
    alt_times = [row["open_time"] for row in alt_1m]
    btc_times = [row["open_time"] for row in btc_1m]
    events: list[dict[str, Any]] = []
    funnel: Counter = Counter()

    for cutoff in cutoffs:
        signal = detect_v15_pair_signal(alt_symbol, alt_1h, btc_1h, cutoff)
        if not signal.get("eligible"):
            funnel[f"reject:{signal.get('reason', 'unknown')}"] += 1
            continue
        funnel["eligible_pair_signal"] += 1
        result = simulate_v15_pair(
            signal,
            _future_slice(alt_1m, alt_times, cutoff),
            _future_slice(btc_1m, btc_times, cutoff),
        )
        events.append(
            {
                "alt_symbol": alt_symbol,
                "pair": f"{alt_symbol}/BTCUSDT",
                "decision_time": cutoff,
                "pair_direction": signal["pair_direction"],
                "entry_z": signal["zscore"],
                "beta": signal["beta"],
                "exit_time": result.exit_time,
                "exit_reason": result.exit_reason,
                "observation": result.observation,
            }
        )
    del alt_bundle, alt_1h, alt_1m
    gc.collect()
    return events, funnel


def _select_cross_section(
    events: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Select the largest absolute residual before using any outcome."""
    grouped: dict[dt.datetime, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["decision_time"]].append(event)
    selected: list[dict[str, Any]] = []
    competing = 0
    for _, candidates in sorted(grouped.items()):
        candidates.sort(
            key=lambda row: (
                -abs(float(row["entry_z"])),
                SYMBOL_ORDER[row["alt_symbol"]],
            )
        )
        selected.append(candidates[0])
        competing += len(candidates) - 1
    return selected, {
        "decision_points_with_signal": len(grouped),
        "weaker_same_scan_pairs_suppressed": competing,
    }


def _apply_global_lifecycle(
    events: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    busy_until: dt.datetime | None = None
    suppressed = 0
    for event in sorted(
        events, key=lambda row: (row["decision_time"], SYMBOL_ORDER[row["alt_symbol"]])
    ):
        if busy_until is not None and event["decision_time"] < busy_until:
            suppressed += 1
            continue
        submitted.append(event)
        if event["observation"].filled and event["exit_time"] is not None:
            busy_until = event["exit_time"]
    return submitted, {
        "scope": "GLOBAL_BECAUSE_EVERY_PAIR_USES_BTC",
        "submitted": len(submitted),
        "suppressed_while_pair_open": suppressed,
    }


def _group(
    events: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], str]
) -> dict[str, Any]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for event in events:
        grouped[str(key(event))].append(event["observation"])
    return {
        name: summarize_observations(rows) for name, rows in sorted(grouped.items())
    }


def _run_schedule(
    name: str,
    hours: tuple[int, ...],
    successful: Mapping[str, Any],
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    cutoffs = decision_times(start, end, hours=hours)
    btc_bundle = load_extended_bundle(
        Path(successful["BTCUSDT"]["manifest"]["cache_dir"])
    )
    events: list[dict[str, Any]] = []
    funnel: Counter = Counter()
    for alt_symbol in ALT_SYMBOLS:
        pair_events, pair_funnel = _collect_pair(
            alt_symbol, successful[alt_symbol], btc_bundle, cutoffs
        )
        events.extend(pair_events)
        funnel.update(pair_funnel)
    del btc_bundle
    gc.collect()

    selected, cross_section = _select_cross_section(events)
    submitted, lifecycle = _apply_global_lifecycle(selected)
    return {
        "schedule": {"name": name, "hours": list(hours), "cutoffs": len(cutoffs)},
        "funnel": dict(funnel),
        "cross_section": cross_section,
        "lifecycle": summarize_observations(
            [event["observation"] for event in submitted]
        ),
        "by_pair": _group(submitted, lambda event: event["pair"]),
        "by_direction": _group(submitted, lambda event: event["pair_direction"]),
        "by_exit_reason": _group(submitted, lambda event: event["exit_reason"]),
        "lifecycle_gate": lifecycle,
        "event_ledger": [
            {
                "pair": event["pair"],
                "decision_time": event["decision_time"],
                "pair_direction": event["pair_direction"],
                "entry_z": event["entry_z"],
                "beta": event["beta"],
                "exit_time": event["exit_time"],
                "exit_reason": event["exit_reason"],
                "r_multiple": event["observation"].r_multiple,
            }
            for event in submitted
        ],
    }


def _cadence_overlap(results: Mapping[str, Any]) -> dict[str, Any]:
    one_h = results["FULL_24H_1H"]["event_ledger"]
    two_h = results["FULL_24H_2H"]["event_ledger"]
    key = lambda event: (event["pair"], event["decision_time"], event["pair_direction"])
    one_index = {key(event): event for event in one_h}
    two_index = {key(event): event for event in two_h}
    common = set(one_index) & set(two_index)
    union = set(one_index) | set(two_index)
    return {
        "definition": "pair + decision_time + pair_direction",
        "full_1h_events": len(one_index),
        "full_2h_events": len(two_index),
        "common_events": len(common),
        "only_full_1h": len(set(one_index) - set(two_index)),
        "only_full_2h": len(set(two_index) - set(one_index)),
        "jaccard": len(common) / len(union) if union else None,
        "common_r_mismatches": sum(
            one_index[item]["r_multiple"] != two_index[item]["r_multiple"]
            for item in common
        ),
    }


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def assess_v15_development(
    results: Mapping[str, Any], cadence: Mapping[str, Any]
) -> dict[str, Any]:
    names = tuple(SCHEDULES)
    metrics = {name: results[name]["lifecycle"] for name in names}
    expectancy = {
        name: _decimal(row.get("expectancy_r")) for name, row in metrics.items()
    }
    profit_factor = {
        name: _decimal(row.get("profit_factor")) for name, row in metrics.items()
    }
    positive_count = sum(
        value is not None and value > 0 for value in expectancy.values()
    )
    pf_count = sum(
        value is not None and value >= DEVELOPMENT_PASS_RULE["profit_factor_threshold"]
        for value in profit_factor.values()
    )
    floor_pass = all(
        value is not None
        and value >= DEVELOPMENT_PASS_RULE["catastrophic_schedule_floor_expectancy_r"]
        for value in expectancy.values()
    )
    positive_ci_count = sum(
        (row.get("expectancy_ci") or {}).get("low") is not None
        and Decimal(str(row["expectancy_ci"]["low"])) > 0
        for row in metrics.values()
    )

    full_1h = results["FULL_24H_1H"]
    terminal_pairs = int(full_1h["lifecycle"].get("trades_with_r") or 0)
    positive_pnls = [
        value
        for row in full_1h["by_pair"].values()
        if (value := _decimal(row.get("pnl_net"))) is not None and value > 0
    ]
    total_positive = sum(positive_pnls, Decimal("0"))
    concentration = (
        max(positive_pnls, default=Decimal("0")) / total_positive
        if total_positive
        else None
    )
    checks = {
        "positive_expectancy_schedules": {
            "actual": positive_count,
            "required": DEVELOPMENT_PASS_RULE["positive_expectancy_schedules_min"],
            "passed": positive_count
            >= DEVELOPMENT_PASS_RULE["positive_expectancy_schedules_min"],
        },
        "profit_factor_schedules": {
            "actual": pf_count,
            "required": DEVELOPMENT_PASS_RULE["profit_factor_schedules_min"],
            "passed": pf_count >= DEVELOPMENT_PASS_RULE["profit_factor_schedules_min"],
        },
        "catastrophic_floor": {"passed": floor_pass},
        "full_1h_terminal_pairs": {
            "actual": terminal_pairs,
            "required": DEVELOPMENT_PASS_RULE["full_24h_1h_min_terminal_pairs"],
            "passed": terminal_pairs
            >= DEVELOPMENT_PASS_RULE["full_24h_1h_min_terminal_pairs"],
        },
        "positive_95pct_ci_low_schedules": {
            "actual": positive_ci_count,
            "required": DEVELOPMENT_PASS_RULE["positive_95pct_ci_low_schedules_min"],
            "passed": positive_ci_count
            >= DEVELOPMENT_PASS_RULE["positive_95pct_ci_low_schedules_min"],
        },
        "single_pair_positive_pnl_concentration": {
            "actual": concentration,
            "maximum": DEVELOPMENT_PASS_RULE["max_single_pair_share_of_positive_pnl"],
            "passed": concentration is not None
            and concentration
            <= DEVELOPMENT_PASS_RULE["max_single_pair_share_of_positive_pnl"],
        },
        "cadence_common_r_mismatches": {
            "actual": int(cadence["common_r_mismatches"]),
            "maximum": DEVELOPMENT_PASS_RULE["common_r_mismatches_max"],
            "passed": int(cadence["common_r_mismatches"])
            <= DEVELOPMENT_PASS_RULE["common_r_mismatches_max"],
        },
    }
    passed = all(check["passed"] for check in checks.values())
    return {
        "classification": "DEVELOPMENT_PASS" if passed else "DEVELOPMENT_FAIL",
        "eligible_to_freeze_for_new_v15_holdout": passed,
        "checks": checks,
        "schedule_metrics": metrics,
        "interpretation": "PASS permits freezing for a new V15-specific holdout; it never promotes live trading.",
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = validate_v15_development_cache_root(Path(args.cache_root))
    summary = json.loads(
        (cache_root / "backfill_summary.json").read_text(encoding="utf-8")
    )
    successful = {
        symbol: (summary.get("results") or {}).get(symbol)
        for symbol in REQUIRED_SYMBOLS
        if ((summary.get("results") or {}).get(symbol) or {}).get("status") == "OK"
    }
    missing = [symbol for symbol in REQUIRED_SYMBOLS if symbol not in successful]
    if missing:
        raise ValueError(
            f"V15 development requires frozen five-symbol universe; missing {missing}"
        )

    window = summary.get("window") or {}
    start = _dt(window["evaluation_start"])
    raw_end = _dt(window["evaluation_end"])
    end = raw_end - MIN_FUTURE_EXECUTION
    results = {
        name: _run_schedule(name, hours, successful, start, end)
        for name, hours in SCHEDULES.items()
    }
    cadence = _cadence_overlap(results)
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": V15_NAME,
        "status": "POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION",
        "read_only": True,
        "reserved_mr_v2_120d_holdout_used": False,
        "evaluation_window": {"start": start, "raw_end": raw_end, "decision_end": end},
        "requested_symbols": list(REQUIRED_SYMBOLS),
        "results": results,
        "frozen_development_pass_rule": DEVELOPMENT_PASS_RULE,
        "cadence_diagnostics": cadence,
        "development_assessment": assess_v15_development(results, cadence),
        "guards": [
            "reserved holdout_mrv2_120d path is rejected",
            "closed aligned 1h candles only",
            "one frozen V15 hypothesis with no symbol/hour/direction outcome filter",
            "largest absolute residual selects same-scan pair before outcomes",
            "one global lifecycle because every pair uses BTC",
            "fees, slippage and conservative funding charged on both legs",
            "development output cannot promote V15",
        ],
    }
    output = (
        Path(args.output) if args.output else cache_root.parent / DEFAULT_OUTPUT_NAME
    )
    write_json_cache(output, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--output")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
