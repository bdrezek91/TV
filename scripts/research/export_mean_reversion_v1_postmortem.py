#!/usr/bin/env python3
"""Export event-level diagnostics from the already-observed MR V1 holdout.

IMPORTANT: this is post-holdout research.  The 2026-04-12..2026-07-10 period
has already been observed and may no longer be treated as pristine validation
data for any strategy derived from this analysis.

The exporter intentionally reproduces the frozen MR V1 signal/execution path,
then writes:
- JSON: full point-in-time W1/W2/W3/W4 context + W5 result for every approved
  Mean Reversion event, including lifecycle-suppressed events;
- CSV: analysis-friendly scalar columns for the same event population.

It changes no strategy threshold and sends no order/database writes.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import gc
import json
from pathlib import Path
from typing import Any, Sequence

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_holdout_v1 import (
    HOLDOUT_DECISION_END,
    HOLDOUT_EVALUATION_END,
    HOLDOUT_EVALUATION_START,
    REQUIRED_SYMBOLS,
    validate_holdout_backfill,
)
from tradingview_mcp.core.backtest.comparison_postmortem_v1 import (
    POSTMORTEM_SCHEMA_VERSION,
    apply_lifecycle_gate,
    build_event_record,
    flatten_event,
    jsonable,
)
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS, decision_times, summarize_observations
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle

SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(0, 24)),
}
SYMBOL_ORDER = {symbol: index for index, symbol in enumerate(REQUIRED_SYMBOLS)}
DEFAULT_ROOT = Path("/app/artifacts/research/backtest_comparison/holdout_90d")
DEFAULT_CACHE_ROOT = DEFAULT_ROOT / "cache"
DEFAULT_HOLDOUT_RESULT = DEFAULT_ROOT / "research_v2_mean_reversion_holdout_v1.json"
DEFAULT_JSON_OUTPUT = DEFAULT_ROOT / "mean_reversion_v1_postmortem_events.json"
DEFAULT_CSV_OUTPUT = DEFAULT_ROOT / "mean_reversion_v1_postmortem_events.csv"


def _future_candles(candles: Sequence[dict[str, Any]], times: Sequence[dt.datetime], cutoff: dt.datetime):
    return candles[bisect.bisect_left(times, cutoff):]


def _collect_symbol_events(
    *,
    schedule: str,
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
    candle_times = [c["open_time"] for c in candles]
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

        for signal in neutral_signals_from_research(chain, approved_only=True):
            if signal.setup_name != "mean_reversion":
                continue
            result = simulate_neutral_signal(signal, _future_candles(candles, candle_times, cutoff))
            events.append(build_event_record(schedule=schedule, signal=signal, result=result, chain=chain))

    return events, derived_btc


def _restore_event_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda event: (event["decision_time"], SYMBOL_ORDER[event["symbol"]]))


def _summary_reproduction_guard(
    *,
    schedule: str,
    events: Sequence[dict[str, Any]],
    expected_holdout: dict[str, Any],
) -> dict[str, Any]:
    submitted = [event for event in events if event["lifecycle"]["submitted"]]
    observations = [event["observation"] for event in submitted]
    actual = summarize_observations(observations)
    expected_result = expected_holdout["results"][schedule]
    expected = expected_result["lifecycle_mean_reversion"]
    checks: dict[str, Any] = {}
    for key in ("trades_with_r", "expectancy_r", "profit_factor", "pnl_net"):
        actual_value = actual.get(key)
        expected_value = expected.get(key)
        passed = str(actual_value) == str(expected_value)
        checks[key] = {"actual": actual_value, "expected": expected_value, "passed": passed}
    count_checks = {
        "approved_event_pool": {
            "actual": len(events),
            "expected": expected_result.get("approved_mean_reversion_event_pool"),
        },
        "lifecycle_submitted": {
            "actual": len(submitted),
            "expected": expected_result.get("lifecycle_submitted"),
        },
    }
    for row in count_checks.values():
        row["passed"] = row["actual"] == row["expected"]
    checks.update(count_checks)
    passed = all(bool(row["passed"]) for row in checks.values())
    if not passed:
        raise RuntimeError(f"post-mortem reproduction mismatch for {schedule}: {jsonable(checks)}")
    return {"passed": True, "checks": checks}


def _run_schedule(
    *,
    schedule: str,
    hours: tuple[int, ...],
    successful: dict[str, Any],
    expected_holdout: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cutoffs = decision_times(HOLDOUT_EVALUATION_START, HOLDOUT_DECISION_END, hours=hours)
    events: list[dict[str, Any]] = []

    btc_events, btc_regimes = _collect_symbol_events(
        schedule=schedule,
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
            schedule=schedule,
            symbol=symbol,
            successful_row=successful[symbol],
            cutoffs=cutoffs,
            btc_regimes=btc_regimes,
        )
        events.extend(symbol_events)
        gc.collect()

    events = _restore_event_order(events)
    events = apply_lifecycle_gate(events, decision_end=HOLDOUT_DECISION_END)
    reproduction = _summary_reproduction_guard(
        schedule=schedule,
        events=events,
        expected_holdout=expected_holdout,
    )
    return events, reproduction


def _write_csv(path: Path, events: Sequence[dict[str, Any]]) -> None:
    rows = [flatten_event(event) for event in events]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = Path(args.cache_root)
    summary = json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))
    guard = validate_holdout_backfill(summary, cache_root)
    if not guard["passed"]:
        raise ValueError("holdout backfill contract failed: " + "; ".join(guard["errors"]))

    expected_path = Path(args.holdout_result)
    expected_holdout = json.loads(expected_path.read_text(encoding="utf-8"))
    if expected_holdout.get("holdout_assessment", {}).get("classification") != "HOLDOUT_FAIL":
        raise ValueError("post-mortem exporter is intentionally restricted to the observed HOLDOUT_FAIL artifact")

    successful = {symbol: summary["results"][symbol] for symbol in REQUIRED_SYMBOLS}
    all_events: list[dict[str, Any]] = []
    reproduction_by_schedule: dict[str, Any] = {}
    counts: dict[str, Any] = {}

    for schedule, hours in SCHEDULES.items():
        events, reproduction = _run_schedule(
            schedule=schedule,
            hours=hours,
            successful=successful,
            expected_holdout=expected_holdout,
        )
        all_events.extend(events)
        reproduction_by_schedule[schedule] = reproduction
        counts[schedule] = {
            "approved_events": len(events),
            "submitted": sum(bool(event["lifecycle"]["submitted"]) for event in events),
            "suppressed_busy": sum(not bool(event["lifecycle"]["submitted"]) for event in events),
        }
        gc.collect()

    payload = {
        "schema_version": POSTMORTEM_SCHEMA_VERSION,
        "status": "POST_HOLDOUT_DIAGNOSTIC_ONLY",
        "source_holdout_classification": "HOLDOUT_FAIL",
        "evaluation_window": {
            "start": HOLDOUT_EVALUATION_START,
            "raw_end": HOLDOUT_EVALUATION_END,
            "decision_end": HOLDOUT_DECISION_END,
        },
        "symbols": list(REQUIRED_SYMBOLS),
        "schedules": {name: list(hours) for name, hours in SCHEDULES.items()},
        "reproduction_guard": {
            "passed": all(item["passed"] for item in reproduction_by_schedule.values()),
            "by_schedule": reproduction_by_schedule,
        },
        "counts": counts,
        "research_warning": (
            "This period has been observed. Any hypothesis, feature filter, symbol/hour selection, or strategy derived from "
            "these events must be treated as development/research and validated only on a new untouched holdout."
        ),
        "events": all_events,
    }

    json_output = Path(args.json_output)
    csv_output = Path(args.csv_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(jsonable(payload), indent=2), encoding="utf-8")
    _write_csv(csv_output, all_events)

    return {
        "schema_version": POSTMORTEM_SCHEMA_VERSION,
        "reproduction_guard": payload["reproduction_guard"],
        "counts": counts,
        "json_output": str(json_output),
        "csv_output": str(csv_output),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--holdout-result", default=str(DEFAULT_HOLDOUT_RESULT))
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--csv-output", default=str(DEFAULT_CSV_OUTPUT))
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(jsonable(main(parse_args())), indent=2))
