#!/usr/bin/env python3
"""Development replay for the single post-holdout Mean Reversion V2 hypothesis.

This is NOT a holdout. MR V2 was derived after observing the MR V1 90-day
HOLDOUT_FAIL, so any period already used by V1 discovery/holdout is development
data from this point forward.

The runner is intentionally narrow:
- existing MR V1 signal geometry, W1/W3/W4 and neutral W5 stay unchanged;
- one V2 eligibility hypothesis only: MR_V2_EXHAUSTION_RANGE_QUALITY;
- no symbol, clock-hour, direction or W3-status filtering;
- memory-safe one-symbol-at-a-time bundle loading;
- opt-in lifecycle numerical-dust repair for V2 only;
- same three cadence schedules for robustness diagnostics.

It can be pointed at either the old 30-day discovery cache or the observed
90-day holdout cache. Results from both are development evidence only. A future
promotion decision requires a new untouched dataset.
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
from statistics import median
from typing import Any, Callable, Sequence

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS, assess_holdout
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
from tradingview_mcp.core.backtest.comparison_mean_reversion_v2 import (
    MR_V2_MAX_OI_CHANGE_1H_PCT,
    MR_V2_NAME,
    MR_V2_REJECT_15M_STRUCTURE,
    MR_V2_REJECT_4H_STRUCTURE,
    estimated_full_fill_round_trip_cost_r,
    evaluate_mean_reversion_v2,
)
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
SYMBOL_ORDER = {symbol: index for index, symbol in enumerate(REQUIRED_SYMBOLS)}
DEFAULT_OUTPUT_NAME = "research_v2_mean_reversion_v2_development.json"


def _dt(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        out = value
    else:
        out = dt.datetime.fromisoformat(str(value))
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


def _future_candles(
    candles: Sequence[dict[str, Any]],
    timestamps: Sequence[dt.datetime],
    cutoff: dt.datetime,
) -> Sequence[dict[str, Any]]:
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _load_backfill(cache_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))
    results = summary.get("results") or {}
    successful = {
        symbol: results.get(symbol)
        for symbol in REQUIRED_SYMBOLS
        if (results.get(symbol) or {}).get("status") == "OK"
        and (results.get(symbol) or {}).get("manifest", {}).get("cache_dir")
    }
    missing = [symbol for symbol in REQUIRED_SYMBOLS if symbol not in successful]
    if missing:
        raise ValueError(f"MR V2 development requires the frozen five-symbol universe; missing successful cache for {missing}")
    return summary, successful


def _window(summary: dict[str, Any], start_arg: str | None, end_arg: str | None) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    window = summary.get("window") or {}
    evaluation_start = _dt(window["evaluation_start"])
    evaluation_end = _dt(window["evaluation_end"])
    decision_end = evaluation_end - MIN_FUTURE_EXECUTION
    if start_arg:
        evaluation_start = max(evaluation_start, _dt(start_arg))
    if end_arg:
        decision_end = min(decision_end, _dt(end_arg))
    if decision_end < evaluation_start:
        raise ValueError("empty development window after start/end clipping")
    return evaluation_start, evaluation_end, decision_end


def _group_summary(events: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(key_fn(event))].append(event)
    return {
        key: {"metrics": summarize_observations([item["observation"] for item in items])}
        for key, items in sorted(grouped.items())
    }


def _cost_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        event["estimated_full_fill_round_trip_cost_r"]
        for event in events
        if event.get("estimated_full_fill_round_trip_cost_r") is not None
    ]
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": sum(values, Decimal("0")) / Decimal(len(values)),
        "median": median(values),
        "min": min(values),
        "max": max(values),
        "note": "decision-time diagnostic only; NOT an MR V2 eligibility threshold",
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

        v2_gate = evaluate_mean_reversion_v2(chain)
        for signal in neutral_signals_from_research(chain, approved_only=True):
            if signal.setup_name != "mean_reversion":
                continue
            result = simulate_neutral_signal(signal, _future_candles(candles, candle_times, cutoff))
            cost_r = estimated_full_fill_round_trip_cost_r(
                direction=signal.direction,
                entry_low=signal.entry_low,
                entry_high=signal.entry_high,
                stop_loss=signal.stop_loss,
            )
            events.append({
                "symbol": symbol,
                "decision_time": signal.decision_time,
                "direction": signal.direction,
                "regime": signal.regime or "UNKNOWN",
                "confirmation_status": str(signal.metadata.get("confirmation_status") or "UNKNOWN"),
                "v2_gate": v2_gate,
                "estimated_full_fill_round_trip_cost_r": cost_r,
                "observation": result.observation,
                "order": result.order,
            })

    return events, derived_btc


def _restore_event_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda event: (event["decision_time"], SYMBOL_ORDER[event["symbol"]]),
    )


def _lifecycle_variant(
    events: list[dict[str, Any]],
    *,
    decision_end: dt.datetime,
    eligible_only: bool,
    close_target_dust: bool,
) -> dict[str, Any]:
    pool = [event for event in events if (event["v2_gate"]["eligible"] if eligible_only else True)]
    gate = SingleSymbolLifecycleGate(close_target_dust=close_target_dust)
    submitted: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for event in pool:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            suppressed.append(event)
            continue
        submitted.append(event)
        gate.occupy_from_order(event["symbol"], event["order"], fallback_end=decision_end)

    rows: list[TradeObservation] = [event["observation"] for event in submitted]
    return {
        "lifecycle_mean_reversion": summarize_observations(rows),
        "lifecycle_submitted": len(submitted),
        "lifecycle_suppressed_busy": len(suppressed),
        "groups": {
            "by_symbol": _group_summary(submitted, lambda event: event["symbol"]),
            "by_direction": _group_summary(submitted, lambda event: event["direction"]),
            "by_w3_status": _group_summary(submitted, lambda event: event["confirmation_status"]),
        },
        "planned_cost_r": _cost_summary(submitted),
        "lifecycle_gate_diagnostics": gate.diagnostics(),
    }


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    successful: dict[str, Any],
    *,
    evaluation_start: dt.datetime,
    decision_end: dt.datetime,
) -> dict[str, Any]:
    cutoffs = decision_times(evaluation_start, decision_end, hours=hours)
    events: list[dict[str, Any]] = []

    btc_events, btc_regimes = _collect_symbol_events(
        symbol="BTCUSDT",
        successful_row=successful["BTCUSDT"],
        cutoffs=cutoffs,
        btc_regimes=None,
    )
    events.extend(btc_events)
    if btc_regimes is None or len(btc_regimes) != len(cutoffs):
        raise RuntimeError("failed to reconstruct BTC regime context")
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
    eligible = [event for event in events if event["v2_gate"]["eligible"]]
    rejections: Counter[str] = Counter()
    for event in events:
        if event["v2_gate"]["eligible"]:
            continue
        for reason in event["v2_gate"]["rejection_reasons"]:
            rejections[str(reason)] += 1

    return {
        "schedule": {
            "name": schedule_name,
            "hours": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(REQUIRED_SYMBOLS),
            "total_symbol_decision_points": len(cutoffs) * len(REQUIRED_SYMBOLS),
        },
        "approved_mr_v1_event_pool": len(events),
        "mr_v2_eligible_event_pool": len(eligible),
        "mr_v2_rejected_event_pool": len(events) - len(eligible),
        "mr_v2_rejections": dict(rejections.most_common()),
        # V1 sensitivity is diagnostic only. Default lifecycle semantics remain
        # frozen; this makes it possible to quantify the dust repair without
        # retroactively rewriting the HOLDOUT_FAIL artifact.
        "mr_v1_original_lifecycle": _lifecycle_variant(
            events,
            decision_end=decision_end,
            eligible_only=False,
            close_target_dust=False,
        ),
        "mr_v1_dust_fixed_sensitivity": _lifecycle_variant(
            events,
            decision_end=decision_end,
            eligible_only=False,
            close_target_dust=True,
        ),
        "mr_v2_exhaustion": _lifecycle_variant(
            events,
            decision_end=decision_end,
            eligible_only=True,
            close_target_dust=True,
        ),
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = Path(args.cache_root)
    summary, successful = _load_backfill(cache_root)
    evaluation_start, evaluation_end, decision_end = _window(summary, args.start, args.end)

    results: dict[str, Any] = {}
    for name, hours in SCHEDULES.items():
        results[name] = _run_schedule(
            name,
            hours,
            successful,
            evaluation_start=evaluation_start,
            decision_end=decision_end,
        )
        gc.collect()

    assessment_input = {
        schedule: payload["mr_v2_exhaustion"]
        for schedule, payload in results.items()
    }
    same_rule_diagnostic = assess_holdout(assessment_input)

    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "candidate_kind": MR_V2_NAME,
        "status": "POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION",
        "read_only": True,
        "evaluation_window": {
            "start": evaluation_start,
            "raw_end": evaluation_end,
            "decision_end": decision_end,
        },
        "requested_symbols": list(REQUIRED_SYMBOLS),
        "frozen_v2_hypothesis": {
            "existing_setup": "mean_reversion from frozen MR V1 chain; W1/W2/W3/W4 unchanged",
            "reject_15m_structure": MR_V2_REJECT_15M_STRUCTURE,
            "reject_4h_structure": MR_V2_REJECT_4H_STRUCTURE,
            "maximum_oi_change_1h_pct": MR_V2_MAX_OI_CHANGE_1H_PCT,
            "futures_unavailable_policy": "FAIL_CLOSED",
            "symbol_filter": None,
            "hour_filter": None,
            "direction_filter": None,
            "w3_status_filter": None,
            "cost_filter": None,
            "lifecycle_target_dust_repair": True,
        },
        "same_old_holdout_rule_development_diagnostic": same_rule_diagnostic,
        "results": results,
        "research_warning": (
            "This is development evidence produced after MR V1 holdout outcomes were observed. "
            "It must not be described as out-of-sample validation. Promotion requires a new untouched holdout."
        ),
        "next_validation_principle": (
            "Run exactly one frozen MR V2 candidate on a new untouched period; do not carry symbol/hour/direction/W3 filters forward."
        ),
    }

    output = Path(args.output) if args.output else (
        cache_root.parent / DEFAULT_OUTPUT_NAME if cache_root.name == "cache" else cache_root / DEFAULT_OUTPUT_NAME
    )
    write_json_cache(output, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
