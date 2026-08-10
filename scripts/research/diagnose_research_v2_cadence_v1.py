#!/usr/bin/env python3
"""Compare Research V2 opportunity funnel under several scan clocks.

Research-only and cache-only. No network, DB writes, paper state, or execution.
This diagnoses whether the current 07..21 Europe/Warsaw clock is suppressing
opportunity count versus true 24/7 crypto scanning.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from tradingview_mcp.core.backtest.comparison_funnel_v1 import ResearchFunnelDiagnostics
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex, build_research_v2_chain_indexed
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS, decision_times
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, write_json_cache

UTC = dt.timezone.utc
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
SCHEDULES = {
    "CURRENT_DAYTIME_2H_07_21": tuple(DEFAULT_SCAN_HOURS),
    "FULL_24H_2H": tuple(range(0, 24, 2)),
    "FULL_24H_1H": tuple(range(0, 24)),
}


def _dt(v: str) -> dt.datetime:
    out = dt.datetime.fromisoformat(v)
    if out.tzinfo is None:
        raise ValueError(f"timezone required: {v}")
    return out.astimezone(UTC)


def _load_backfill(cache_root: Path) -> dict:
    return json.loads((cache_root / "backfill_summary.json").read_text(encoding="utf-8"))


def _selected_successful(backfill: dict, symbols_arg: str | None) -> tuple[list[str], dict]:
    successful = {
        symbol: row for symbol, row in (backfill.get("results") or {}).items()
        if row.get("status") == "OK" and row.get("manifest", {}).get("cache_dir")
    }
    if symbols_arg:
        selected = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    else:
        selected = list(successful)
    missing = [s for s in selected if s not in successful]
    if missing:
        raise ValueError(f"missing successful cache for: {missing}")
    return selected, successful


def _run_schedule(name: str, hours: tuple[int, ...], requested: list[str], successful: dict, start: dt.datetime, end: dt.datetime) -> dict:
    context_symbols = list(requested)
    if "BTCUSDT" not in context_symbols and "BTCUSDT" in successful:
        context_symbols = ["BTCUSDT"] + context_symbols
    elif "BTCUSDT" in context_symbols:
        context_symbols = ["BTCUSDT"] + [s for s in context_symbols if s != "BTCUSDT"]

    bundles = {s: load_extended_bundle(Path(successful[s]["manifest"]["cache_dir"])) for s in context_symbols}
    indexes = {s: HistoricalWindowIndex(b.inputs) for s, b in bundles.items()}
    states = {s: None for s in context_symbols}
    funnel = ResearchFunnelDiagnostics()
    cutoffs = decision_times(start, end, hours=hours)

    for cutoff in cutoffs:
        btc_regime = None
        chains = {}
        for symbol in context_symbols:
            chain = build_research_v2_chain_indexed(
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
            funnel.record(chains[symbol])

    result = funnel.to_dict()
    result["schedule"] = {
        "name": name,
        "local_hours_europe_warsaw": list(hours),
        "cutoffs_per_symbol": len(cutoffs),
        "symbols": len(requested),
        "total_symbol_decision_points": len(cutoffs) * len(requested),
    }
    return result


def main(args) -> dict:
    cache_root = Path(args.cache_root)
    backfill = _load_backfill(cache_root)
    requested, successful = _selected_successful(backfill, args.symbols)
    window = backfill.get("window") or {}
    start = _dt(window["evaluation_start"])
    evaluation_end = _dt(window["evaluation_end"])
    end = evaluation_end - MIN_FUTURE_EXECUTION
    if args.start:
        start = max(start, _dt(args.start))
    if args.end:
        end = min(end, _dt(args.end))

    results = {
        name: _run_schedule(name, hours, requested, successful, start, end)
        for name, hours in SCHEDULES.items()
    }
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "operation": "RESEARCH_V2_CADENCE_SENSITIVITY",
        "read_only": True,
        "evaluation_window": {"start": start.isoformat(), "end": end.isoformat()},
        "requested_symbols": requested,
        "results": results,
        "interpretation_guard": (
            "More candidates are not automatically better. This diagnostic only measures opportunity/funnel sensitivity to scan cadence; outcome quality still requires W5 replay."
        ),
    }
    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / "research_v2_cadence_sensitivity.json", payload)
    return payload


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    p.add_argument("--symbols", help="comma-separated subset of successful backfill symbols")
    p.add_argument("--start", help="optional timezone-aware ISO clamp")
    p.add_argument("--end", help="optional timezone-aware ISO clamp")
    return p.parse_args()


if __name__ == "__main__":
    print(json.dumps(main(parse_args()), indent=2, default=str))
