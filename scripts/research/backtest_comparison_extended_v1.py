#!/usr/bin/env python3
"""Extended degraded replay: Research V2 baseline + TV shadow stratification.

Consumes the latest ``backfill_extended_history_v1.py`` cache summary. Because
validated historical orderbook/liquidation adapters are not yet available, this
runner explicitly forbids an exact legacy comparison and labels every result
DEGRADED. It is useful for a longer Research V2 / classic-TA shadow hypothesis,
not as the final TV-vs-bot verdict.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import (
    HistoricalWindowIndex,
    build_research_v2_chain_indexed,
)
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_v1 import TradeObservation, decision_times, summarize_observations
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, write_json_cache
from tradingview_mcp.core.backtest.tv_shadow_v1 import evaluate_tv_shadow


UTC = dt.timezone.utc
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
OUT_NAME = "extended_research_v2_tv_shadow_summary.json"


def _dt(v: str) -> dt.datetime:
    out = dt.datetime.fromisoformat(v)
    if out.tzinfo is None:
        raise ValueError(f"timezone required: {v}")
    return out.astimezone(UTC)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _future_candles(candles: list[dict], timestamps: list[dt.datetime], cutoff: dt.datetime) -> list[dict]:
    idx = bisect.bisect_left(timestamps, cutoff)
    return candles[idx:]


def _summary_by_setup(rows: list[TradeObservation]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for row in rows:
        grouped[row.setup_name].append(row)
    return {name: summarize_observations(items) for name, items in grouped.items()}


def _counterfactual_summary(pairs: list[tuple[str, TradeObservation]]) -> dict[str, Any]:
    baseline = [obs for _, obs in pairs]
    drop_contradicted = [obs for status, obs in pairs if status != "CONTRADICTED"]
    confirmed_or_weak = [obs for status, obs in pairs if status in {"CONFIRMED", "WEAK_CONFIRMATION"}]
    # These policies are fixed in code before seeing outcomes. They do not tune
    # thresholds or modify the historical signals/execution.
    return {
        "BASELINE_ALL_RESEARCH_V2": summarize_observations(baseline),
        "HYPOTHETICAL_DROP_TV_CONTRADICTED": summarize_observations(drop_contradicted),
        "HYPOTHETICAL_KEEP_TV_CONFIRMED_OR_WEAK": summarize_observations(confirmed_or_weak),
        "note": "counterfactual fixed filters only; TV shadow had zero effect on original W4/W5 decisions",
    }


def _trade_row(signal, shadow, result, chain) -> dict[str, Any]:
    return {
        "symbol": signal.symbol,
        "decision_time": signal.decision_time,
        "setup": signal.setup_name,
        "direction": signal.direction,
        "regime": signal.regime,
        "w3_confirmation": signal.metadata.get("confirmation_status"),
        "native_w4_decision": signal.metadata.get("native_risk_decision"),
        "tv_shadow_status": shadow.status,
        "tv_shadow_score": shadow.score,
        "tv_shadow_confidence": shadow.confidence,
        "tv_history_kind": shadow.history_kind,
        "tv_can_affect_decision": shadow.can_affect_decision,
        "data_quality_score": chain["data_quality"].get("data_quality_score"),
        "paper_status": result.order["status"],
        "filled": bool(result.order.get("entry_time")),
        "r_corrected": result.r_multiple_corrected,
        "corrected_net_pnl": result.corrected_net_pnl,
        "fees_paid": result.order.get("fees_paid"),
        "slippage_cost": result.order.get("slippage_cost"),
        "funding_paid": result.order.get("funding_paid"),
    }


def _load_backfill_summary(cache_root: Path) -> dict[str, Any]:
    path = cache_root / "backfill_summary.json"
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path}; run scripts/research/backfill_extended_history_v1.py first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


async def run(args) -> dict[str, Any]:
    cache_root = Path(args.cache_root)
    backfill = _load_backfill_summary(cache_root)
    successful = {
        symbol: row for symbol, row in (backfill.get("results") or {}).items()
        if row.get("status") == "OK" and row.get("manifest", {}).get("cache_dir")
    }
    if args.symbols:
        requested = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        requested = list(successful)
    missing = [s for s in requested if s not in successful]
    if missing:
        return {"status": "MISSING_SUCCESSFUL_CACHE", "symbols": missing}

    # BTC is evaluated internally first when available so altcoin W1/W3 gets
    # the same BTC-regime context as the live research pipeline.
    context_symbols = list(requested)
    if "BTCUSDT" not in context_symbols and "BTCUSDT" in successful:
        context_symbols = ["BTCUSDT"] + context_symbols
    elif "BTCUSDT" in context_symbols:
        context_symbols = ["BTCUSDT"] + [s for s in context_symbols if s != "BTCUSDT"]

    bundles = {
        s: load_extended_bundle(Path(successful[s]["manifest"]["cache_dir"]))
        for s in context_symbols
    }
    incapable = [s for s, b in bundles.items() if not b.capabilities.get("RESEARCH_V2_DEGRADED")]
    if incapable:
        return {
            "status": "INSUFFICIENT_EXTENDED_RESEARCH_INPUTS",
            "symbols": incapable,
            "capabilities": {s: bundles[s].capabilities for s in incapable},
        }

    window = backfill.get("window") or {}
    evaluation_start = _dt(window["evaluation_start"])
    evaluation_end = _dt(window["evaluation_end"])
    safe_end = evaluation_end - MIN_FUTURE_EXECUTION
    if args.start:
        evaluation_start = max(evaluation_start, _dt(args.start))
    if args.end:
        safe_end = min(safe_end, _dt(args.end))
    cutoffs = decision_times(evaluation_start, safe_end)
    if not cutoffs:
        return {"status": "NO_DECISION_TIMES", "start": evaluation_start, "end": safe_end}

    indexes = {s: HistoricalWindowIndex(b.inputs) for s, b in bundles.items()}
    candle_lists = {s: list(b.inputs.candles_1m) for s, b in bundles.items()}
    candle_times = {s: [c["open_time"] for c in candle_lists[s]] for s in bundles}
    states = {s: None for s in context_symbols}

    observations: list[TradeObservation] = []
    shadow_pairs: list[tuple[str, TradeObservation]] = []
    by_shadow: dict[str, list[TradeObservation]] = defaultdict(list)
    shadow_counts = Counter()
    decisions: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for cutoff in cutoffs:
        btc_regime = None
        chains: dict[str, dict] = {}
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
            chain = chains[symbol]
            signals = neutral_signals_from_research(chain, approved_only=True)
            signal_summaries = []
            for signal in signals:
                shadow = evaluate_tv_shadow(signal, chain["_windows"])
                future = _future_candles(candle_lists[symbol], candle_times[symbol], cutoff)
                result = simulate_neutral_signal(signal, future)
                observations.append(result.observation)
                shadow_pairs.append((shadow.status, result.observation))
                by_shadow[shadow.status].append(result.observation)
                shadow_counts[shadow.status] += 1
                trade_rows.append(_trade_row(signal, shadow, result, chain))
                signal_summaries.append({
                    "setup": signal.setup_name,
                    "direction": signal.direction,
                    "w3": signal.metadata.get("confirmation_status"),
                    "w4": signal.metadata.get("native_risk_decision"),
                    "tv_shadow": shadow.to_dict(),
                    "paper_status": result.order["status"],
                })
            decisions.append({
                "cutoff": cutoff,
                "symbol": symbol,
                "regime": chain["regime"].get("primary_regime"),
                "data_quality": chain["data_quality"],
                "signals": signal_summaries,
            })

    by_status_summary = {
        status: summarize_observations(rows) for status, rows in sorted(by_shadow.items())
    }
    capability_matrix = {s: dict(b.capabilities) for s, b in bundles.items()}
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "run_kind": "EXTENDED_DEGRADED_RESEARCH_V2_TV_SHADOW",
        "status": "COMPLETE_DEGRADED_REPLAY",
        "final_tv_vs_bot_verdict_allowed": False,
        "reason_final_verdict_forbidden": (
            "historical orderbook/liquidation adapters not validated; legacy exact replay is forbidden"
        ),
        "evaluation_window": {"start": evaluation_start, "end": safe_end},
        "decision_points_per_symbol": len(cutoffs),
        "requested_symbols": requested,
        "context_symbols": context_symbols,
        "capabilities": capability_matrix,
        "baseline_research_v2": summarize_observations(observations),
        "baseline_by_setup": _summary_by_setup(observations),
        "tv_shadow_counts": dict(shadow_counts),
        "tv_shadow_outcomes": by_status_summary,
        "predeclared_counterfactual_filters": _counterfactual_summary(shadow_pairs),
        "tv_shadow_contract": {
            "can_affect_decision": False,
            "history_kind": "TV_RECONSTRUCTED_CLASSIC_TA_V1",
            "not_independent_market_data": True,
            "purpose": "test whether TV-style classic TA tags stratify Research V2 outcomes",
        },
        "limitations": [
            "DEGRADED_NO_HISTORICAL_ORDERBOOK",
            "DEGRADED_NO_HISTORICAL_LIQUIDATIONS",
            "LEGACY_AS_IS_NOT_RUN_EXTENDED",
            "LEGACY_FIXED_TV_NOT_RUN_EXTENDED",
            "LIQUIDATION_REVERSAL_NOT_VALIDATED_EXTENDED",
            "TV_RECONSTRUCTED_NOT_TRUE_POINT_IN_TIME_TRADINGVIEW",
        ],
    }

    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / OUT_NAME, payload)
    with (out_dir / "extended_research_v2_tv_shadow_decisions.jsonl").open("w", encoding="utf-8") as fh:
        for row in decisions:
            fh.write(json.dumps(_jsonable(row)) + "\n")
    if trade_rows:
        with (out_dir / "extended_research_v2_tv_shadow_trades.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(trade_rows[0].keys()))
            writer.writeheader()
            writer.writerows([{k: _jsonable(v) for k, v in row.items()} for row in trade_rows])
    return payload


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    p.add_argument("--symbols", help="comma-separated subset of successful backfill symbols")
    p.add_argument("--start", help="optional timezone-aware ISO clamp inside evaluation window")
    p.add_argument("--end", help="optional timezone-aware ISO clamp; 12h future execution reserve still applies")
    return p.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(__import__("asyncio").run(run(parse_args()))), indent=2))
