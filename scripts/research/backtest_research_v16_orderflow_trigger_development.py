"""Development-only runner for frozen V16 order-flow-triggered, regime-gated
single-instrument strategy.

The runner reads already-observed extended-history caches, refuses the
reserved MR V2 holdout, and cannot promote V16. Each of the five frozen
symbols is evaluated independently (single-leg, max one open position per
symbol at a time); BTC's own features are reused as the `btc_regime` input
when scoring the other four symbols and as its own tradable instrument in
its own right.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_orderflow_trigger_v16 import (
    MAX_HOLD_HOURS,
    V16_NAME,
    V16RawInputs,
    detect_v16_signal,
    simulate_v16_trade,
    validate_v16_development_cache_root,
)
from tradingview_mcp.core.backtest.comparison_v1 import (
    DEFAULT_SCAN_HOURS,
    TradeObservation,
    decision_times,
    summarize_observations,
)
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
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
DEFAULT_OUTPUT_NAME = "research_v16_orderflow_trigger_development.json"
DEVELOPMENT_PASS_RULE = {
    "positive_expectancy_schedules_min": 2,
    "profit_factor_threshold": Decimal("1.15"),
    "profit_factor_schedules_min": 2,
    "catastrophic_schedule_floor_expectancy_r": Decimal("-0.10"),
    "full_24h_1h_min_terminal_positions": 100,
    "positive_95pct_ci_low_schedules_min": 1,
    "max_single_symbol_share_of_positive_pnl": Decimal("0.50"),
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


def _inputs_from_bundle(bundle: Any) -> V16RawInputs:
    return V16RawInputs(
        candles_4h=list(bundle.inputs.candles_4h),
        candles_1h=list(bundle.inputs.candles_1h),
        candles_15m=list(bundle.inputs.candles_15m),
        trades_1m=list(bundle.inputs.trades_1m),
        derivatives=list(bundle.inputs.derivatives),
    )


def _collect_symbol(
    symbol: str,
    symbol_row: Mapping[str, Any],
    btc_inputs: V16RawInputs,
    cutoffs: list[dt.datetime],
) -> tuple[list[dict[str, Any]], Counter]:
    bundle = load_extended_bundle(Path(symbol_row["manifest"]["cache_dir"]))
    inputs = _inputs_from_bundle(bundle)
    del bundle
    gc.collect()

    is_btc = symbol == "BTCUSDT"
    events: list[dict[str, Any]] = []
    funnel: Counter = Counter()
    busy_until: dt.datetime | None = None

    for cutoff in cutoffs:
        if busy_until is not None and cutoff < busy_until:
            funnel["skipped_position_already_open"] += 1
            continue
        signal = detect_v16_signal(symbol, cutoff, inputs, None if is_btc else btc_inputs)
        if not signal.get("eligible"):
            funnel[f"reject:{signal.get('reason', 'unknown')}"] += 1
            continue
        funnel["eligible_signal"] += 1
        result = simulate_v16_trade(signal, inputs, None if is_btc else btc_inputs)
        events.append(
            {
                "symbol": symbol,
                "decision_time": cutoff,
                "direction": signal["direction"],
                "primary_regime": signal["primary_regime"],
                "btc_regime": signal["btc_regime"],
                "entry_price": signal["entry_price"],
                "stop_price": signal["stop_price"],
                "net_independent_score": signal["net_independent_score"],
                "exit_time": result.exit_time,
                "exit_reason": result.exit_reason,
                "observation": result.observation,
            }
        )
        if result.observation.filled and result.exit_time is not None:
            busy_until = result.exit_time
    del inputs
    gc.collect()
    return events, funnel


def _group(
    events: list[dict[str, Any]], key: Callable[[dict[str, Any]], str]
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
    btc_bundle = load_extended_bundle(Path(successful["BTCUSDT"]["manifest"]["cache_dir"]))
    btc_inputs = _inputs_from_bundle(btc_bundle)
    del btc_bundle
    gc.collect()

    events: list[dict[str, Any]] = []
    funnel: Counter = Counter()
    for symbol in REQUIRED_SYMBOLS:
        symbol_events, symbol_funnel = _collect_symbol(
            symbol, successful[symbol], btc_inputs, cutoffs
        )
        events.extend(symbol_events)
        funnel.update(symbol_funnel)
    del btc_inputs
    gc.collect()

    events.sort(key=lambda e: (e["decision_time"], SYMBOL_ORDER[e["symbol"]]))
    return {
        "schedule": {"name": name, "hours": list(hours), "cutoffs": len(cutoffs)},
        "funnel": dict(funnel),
        "lifecycle": summarize_observations([event["observation"] for event in events]),
        "by_symbol": _group(events, lambda event: event["symbol"]),
        "by_direction": _group(events, lambda event: event["direction"]),
        "by_exit_reason": _group(events, lambda event: event["exit_reason"]),
        "event_ledger": [
            {
                "symbol": event["symbol"],
                "decision_time": event["decision_time"],
                "direction": event["direction"],
                "primary_regime": event["primary_regime"],
                "entry_price": event["entry_price"],
                "stop_price": event["stop_price"],
                "exit_time": event["exit_time"],
                "exit_reason": event["exit_reason"],
                "r_multiple": event["observation"].r_multiple,
            }
            for event in events
        ],
    }


def _cadence_overlap(results: Mapping[str, Any]) -> dict[str, Any]:
    one_h = results["FULL_24H_1H"]["event_ledger"]
    two_h = results["FULL_24H_2H"]["event_ledger"]
    key = lambda event: (event["symbol"], event["decision_time"], event["direction"])
    one_index = {key(event): event for event in one_h}
    two_index = {key(event): event for event in two_h}
    common = set(one_index) & set(two_index)
    union = set(one_index) | set(two_index)
    return {
        "definition": "symbol + decision_time + direction",
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


def assess_v16_development(
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
    terminal_positions = int(full_1h["lifecycle"].get("trades_with_r") or 0)
    positive_pnls = [
        value
        for row in full_1h["by_symbol"].values()
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
        "full_1h_terminal_positions": {
            "actual": terminal_positions,
            "required": DEVELOPMENT_PASS_RULE["full_24h_1h_min_terminal_positions"],
            "passed": terminal_positions
            >= DEVELOPMENT_PASS_RULE["full_24h_1h_min_terminal_positions"],
        },
        "positive_95pct_ci_low_schedules": {
            "actual": positive_ci_count,
            "required": DEVELOPMENT_PASS_RULE["positive_95pct_ci_low_schedules_min"],
            "passed": positive_ci_count
            >= DEVELOPMENT_PASS_RULE["positive_95pct_ci_low_schedules_min"],
        },
        "single_symbol_positive_pnl_concentration": {
            "actual": concentration,
            "maximum": DEVELOPMENT_PASS_RULE["max_single_symbol_share_of_positive_pnl"],
            "passed": concentration is not None
            and concentration
            <= DEVELOPMENT_PASS_RULE["max_single_symbol_share_of_positive_pnl"],
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
        "eligible_to_freeze_for_new_v16_holdout": passed,
        "checks": checks,
        "schedule_metrics": metrics,
        "interpretation": "PASS permits freezing for a new V16-specific holdout; it never promotes live trading.",
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = validate_v16_development_cache_root(Path(args.cache_root))
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
            f"V16 development requires frozen five-symbol universe; missing {missing}"
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
        "candidate_kind": V16_NAME,
        "status": "POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION",
        "read_only": True,
        "reserved_mr_v2_120d_holdout_used": False,
        "evaluation_window": {"start": start, "raw_end": raw_end, "decision_end": end},
        "requested_symbols": list(REQUIRED_SYMBOLS),
        "results": results,
        "frozen_development_pass_rule": DEVELOPMENT_PASS_RULE,
        "cadence_diagnostics": cadence,
        "development_assessment": assess_v16_development(results, cadence),
        "guards": [
            "reserved holdout_mrv2_120d path is rejected",
            "closed 1h candles / trade aggregates / derivatives only, no lookahead",
            "one frozen V16 hypothesis, single-leg, five symbols evaluated independently",
            "max one open position per symbol at a time; no cross-symbol suppression",
            "orderbook and liquidations are always unavailable in this dev cache (documented gap)",
            "entry (order-flow) and stop (ATR on price) are computed from disjoint variables",
            "fees, slippage and conservative funding charged on the single leg",
            "development output cannot promote V16",
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
