"""Research V2 Candidate V4: forensic breakdown of mean-reversion lifecycle trades.

Read-only research diagnostic built on the same ALIGNED_TARGETS candidate chain,
current W3/W4 rules, lifecycle gate and W5 execution semantics used by Candidate V3.
It does not tune thresholds or promote any production behavior.

The report isolates mean_reversion and breaks realized lifecycle outcomes down by:
- symbol
- direction
- W1 primary regime
- W3 confirmation status
- local decision hour (Europe/Warsaw)
- local weekday
- BTC W1 regime at decision time
- symbol x direction
- W3 x direction

MAE/MFE distributions are also reported when available.  All group metrics are
computed from the same lifecycle-aware trade pool, so overlapping groups are
for diagnosis only and must not be summed as independent samples.
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
from zoneinfo import ZoneInfo

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS, TradeObservation, decision_times, summarize_observations
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, write_json_cache

UTC = dt.timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(24)),
}
WEEKDAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


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


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal("2")


def _excursion_summary(rows: list[TradeObservation]) -> dict[str, Any]:
    mae = [r.mae_r for r in rows if r.mae_r is not None]
    mfe = [r.mfe_r for r in rows if r.mfe_r is not None]

    def describe(values: list[Decimal]) -> dict[str, Any]:
        if not values:
            return {"count": 0, "min": None, "median": None, "max": None, "mean": None}
        return {
            "count": len(values),
            "min": min(values),
            "median": _median(values),
            "max": max(values),
            "mean": sum(values, Decimal("0")) / Decimal(len(values)),
        }

    return {"mae_r": describe(mae), "mfe_r": describe(mfe)}


def _group_summary(events: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(key_fn(event))].append(event)

    out: dict[str, Any] = {}
    for key, items in sorted(grouped.items()):
        observations = [item["observation"] for item in items]
        wins = sum(1 for row in observations if row.r_multiple is not None and row.r_multiple > 0)
        losses = sum(1 for row in observations if row.r_multiple is not None and row.r_multiple <= 0)
        out[key] = {
            "metrics": summarize_observations(observations),
            "terminal_r_count": wins + losses,
            "wins": wins,
            "losses_or_flat": losses,
            "excursions": _excursion_summary(observations),
        }
    return out


def _rank_groups(groups: dict[str, Any], *, min_trades: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, payload in groups.items():
        metrics = payload["metrics"]
        n = int(metrics.get("trades_with_r") or 0)
        if n < min_trades:
            continue
        expectancy = metrics.get("expectancy_r")
        rows.append({
            "group": key,
            "trades_with_r": n,
            "win_rate": metrics.get("win_rate"),
            "expectancy_r": expectancy,
            "profit_factor": metrics.get("profit_factor"),
            "pnl_net": metrics.get("pnl_net"),
        })
    rows.sort(key=lambda row: float(row["expectancy_r"]) if row["expectancy_r"] is not None else float("-inf"), reverse=True)
    return rows


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    requested: list[str],
    successful: dict,
    start: dt.datetime,
    end: dt.datetime,
    min_rank_trades: int,
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

    approved_mean_reversion_events: list[dict[str, Any]] = []
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
            for signal in neutral_signals_from_research(chain, approved_only=True):
                if signal.setup_name != "mean_reversion":
                    continue
                future = _future_candles(candles[symbol], candle_times[symbol], cutoff)
                result = simulate_neutral_signal(signal, future)
                local = signal.decision_time.astimezone(WARSAW)
                approved_mean_reversion_events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "local_hour": local.hour,
                    "local_weekday": WEEKDAY_NAMES[local.weekday()],
                    "direction": signal.direction,
                    "regime": signal.regime or "UNKNOWN",
                    "btc_regime": btc_regime or "UNKNOWN",
                    "confirmation_status": str(signal.metadata.get("confirmation_status") or "UNKNOWN"),
                    "observation": result.observation,
                    "order": result.order,
                })

    gate = SingleSymbolLifecycleGate()
    lifecycle_events: list[dict[str, Any]] = []
    suppressed_busy: list[dict[str, Any]] = []
    for event in approved_mean_reversion_events:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            suppressed_busy.append(event)
            continue
        lifecycle_events.append(event)
        gate.occupy_from_order(event["symbol"], event["order"], fallback_end=end)

    lifecycle_rows = [event["observation"] for event in lifecycle_events]
    groups = {
        "by_symbol": _group_summary(lifecycle_events, lambda e: e["symbol"]),
        "by_direction": _group_summary(lifecycle_events, lambda e: e["direction"]),
        "by_w1_regime": _group_summary(lifecycle_events, lambda e: e["regime"]),
        "by_w3_status": _group_summary(lifecycle_events, lambda e: e["confirmation_status"]),
        "by_local_hour": _group_summary(lifecycle_events, lambda e: f"{e['local_hour']:02d}:00"),
        "by_local_weekday": _group_summary(lifecycle_events, lambda e: e["local_weekday"]),
        "by_btc_regime": _group_summary(lifecycle_events, lambda e: e["btc_regime"]),
        "by_symbol_direction": _group_summary(lifecycle_events, lambda e: f"{e['symbol']}|{e['direction']}"),
        "by_w3_direction": _group_summary(lifecycle_events, lambda e: f"{e['confirmation_status']}|{e['direction']}"),
    }

    rankings = {
        name: _rank_groups(payload, min_trades=min_rank_trades)
        for name, payload in groups.items()
    }

    suppressed_counts: dict[str, int] = defaultdict(int)
    for event in suppressed_busy:
        suppressed_counts[event["symbol"]] += 1

    return {
        "schedule": {
            "name": schedule_name,
            "local_hours_europe_warsaw": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(requested),
            "total_symbol_decision_points": len(cutoffs) * len(requested),
        },
        "approved_mean_reversion_event_pool": len(approved_mean_reversion_events),
        "lifecycle_mean_reversion": summarize_observations(lifecycle_rows),
        "lifecycle_excursions": _excursion_summary(lifecycle_rows),
        "lifecycle_submitted": len(lifecycle_events),
        "lifecycle_suppressed_busy": len(suppressed_busy),
        "lifecycle_suppressed_busy_by_symbol": dict(sorted(suppressed_counts.items())),
        "groups": groups,
        "rankings_min_trades": min_rank_trades,
        "rankings": rankings,
        "lifecycle_gate_diagnostics": gate.diagnostics(),
        "interpretation_contract": (
            "All breakdowns describe the same lifecycle-aware mean_reversion pool. "
            "They are diagnostic slices, not independent strategies. Small groups must not be promoted from this window alone."
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
        "candidate_kind": "RESEARCH_V2_CANDIDATE_V4_MEAN_REVERSION_FORENSICS",
        "status": "RESEARCH_ONLY_NOT_PROMOTED",
        "read_only": True,
        "candidate_geometry": "ALIGNED_TARGETS",
        "setup_scope": ["mean_reversion"],
        "evaluation_window": {"start": start, "end": end},
        "requested_symbols": requested,
        "results": {
            schedule_name: _run_schedule(
                schedule_name,
                hours,
                requested,
                successful,
                start,
                end,
                args.min_rank_trades,
            )
            for schedule_name, hours in schedules.items()
        },
        "guards": [
            "production Research V2 is unchanged",
            "ALIGNED_TARGETS geometry is unchanged",
            "W1/W2/W3/W4 thresholds are unchanged",
            "only mean_reversion approved events are eligible for lifecycle submission",
            "W5 execution, fees, slippage and funding assumptions are unchanged",
            "no historical orderbook/liquidation values are fabricated",
            "all inputs are decision-time or earlier",
            "small subgroup rankings are exploratory and must be validated out of sample",
        ],
    }
    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / "research_v2_candidate_v4_mean_reversion_forensics.json", payload)
    return payload


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--schedule", choices=sorted(SCHEDULES))
    parser.add_argument("--min-rank-trades", type=int, default=3)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
