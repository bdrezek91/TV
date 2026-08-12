"""Strict local A/B/C + TV-shadow runner for BACKTEST_COMPARISON_V1.

Fails closed unless every selected symbol has a common point-in-time window
with enough warm-up and at least 12 hours of future 1m execution data after
the last decision cutoff.

READ-ONLY relative to production: DB reads only, no paper-state loads/saves,
no signal snapshot writes, no exchange order endpoints. TV shadow is strictly
observational and cannot change W4/W5 decisions.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tradingview_mcp.core.backtest.comparison_diagnostics_v1 import (
    paired_setup_families,
    signal_set_category,
)
from tradingview_mcp.core.backtest.comparison_execution_v1 import (
    EntryExitIntrabarPolicy,
    TargetSchedulePolicy,
    production_two_target_residual_fraction,
    simulate_neutral_signal,
)
from tradingview_mcp.core.backtest.comparison_history_v1 import (
    HistoricalInputs,
    build_legacy_chain,
    build_research_v2_chain,
    neutral_signals_from_legacy,
    neutral_signals_from_research,
    windows_as_of,
)
from tradingview_mcp.core.backtest.comparison_v1 import (
    Variant,
    decision_times,
    summarize_observations,
)
from tradingview_mcp.core.backtest.tv_shadow_v1 import evaluate_tv_shadow
from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.repositories import query_repository as qr
from tradingview_mcp.core.database.session import session_scope
from tradingview_mcp.core.services import regime_service_v2 as rs

OUT = Path("/app/artifacts/research/backtest_comparison")
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
MIN_WARMUP_CANDLE_BARS = 50
MIN_WARMUP_TRADE_BARS = 10


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, Variant):
        return v.value
    if is_dataclass(v):
        return _jsonable(asdict(v))
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


def _availability_span(rows: Sequence[dict], key: str, duration: dt.timedelta = dt.timedelta(0)) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "earliest_available": None, "latest_available": None}
    available = sorted(r[key] + duration for r in rows)
    return {
        "count": len(rows),
        "earliest_available": available[0],
        "latest_available": available[-1],
    }


def _warmup_available(rows: Sequence[dict], key: str, duration: dt.timedelta, bars: int) -> dt.datetime | None:
    ordered = sorted(rows, key=lambda r: r[key])
    if len(ordered) < bars:
        return None
    return ordered[bars - 1][key] + duration


def coverage_from_inputs(data: HistoricalInputs) -> dict[str, Any]:
    """Availability-time coverage, not raw open-time coverage."""
    spans = {
        "4h": _availability_span(data.candles_4h, "open_time", dt.timedelta(hours=4)),
        "1h": _availability_span(data.candles_1h, "open_time", dt.timedelta(hours=1)),
        "15m": _availability_span(data.candles_15m, "open_time", dt.timedelta(minutes=15)),
        "candles_1m": _availability_span(data.candles_1m, "open_time", dt.timedelta(minutes=1)),
        "trades_1m": _availability_span(data.trades_1m, "bucket_start", dt.timedelta(minutes=1)),
        "liquidations_1m": _availability_span(data.liquidations_1m, "bucket_start", dt.timedelta(minutes=1)),
        "orderbook": _availability_span(data.orderbook, "source_timestamp"),
        "derivatives": _availability_span(data.derivatives, "source_timestamp"),
    }
    warmups = {
        "4h_ema50": _warmup_available(data.candles_4h, "open_time", dt.timedelta(hours=4), MIN_WARMUP_CANDLE_BARS),
        "1h_ema50": _warmup_available(data.candles_1h, "open_time", dt.timedelta(hours=1), MIN_WARMUP_CANDLE_BARS),
        "15m_ema50": _warmup_available(data.candles_15m, "open_time", dt.timedelta(minutes=15), MIN_WARMUP_CANDLE_BARS),
        "trades_10": _warmup_available(data.trades_1m, "bucket_start", dt.timedelta(minutes=1), MIN_WARMUP_TRADE_BARS),
    }
    missing = [name for name, span in spans.items() if span["count"] == 0]
    missing_warmup = [name for name, value in warmups.items() if value is None]

    decision_sources = ("4h", "1h", "15m", "trades_1m", "liquidations_1m", "orderbook", "derivatives")
    if missing or missing_warmup:
        return {
            "complete": False,
            "missing_sources": missing,
            "missing_warmup": missing_warmup,
            "spans": spans,
            "warmups": warmups,
            "decision_start": None,
            "decision_end": None,
        }

    warmup_start = max(v for v in warmups.values() if v is not None)
    raw_start = max(spans[name]["earliest_available"] for name in decision_sources)
    decision_start = max(warmup_start, raw_start)
    decision_end_data = min(spans[name]["latest_available"] for name in decision_sources)
    execution_safe_end = spans["candles_1m"]["latest_available"] - MIN_FUTURE_EXECUTION
    decision_end = min(decision_end_data, execution_safe_end)
    complete = decision_end >= decision_start
    return {
        "complete": complete,
        "missing_sources": [],
        "missing_warmup": [],
        "spans": spans,
        "warmups": warmups,
        "decision_start": decision_start,
        "decision_end": decision_end,
        "minimum_future_execution_hours": MIN_FUTURE_EXECUTION.total_seconds() / 3600,
    }


def global_exact_window(coverage_by_symbol: Mapping[str, Mapping[str, Any]]) -> tuple[dt.datetime | None, dt.datetime | None, list[str]]:
    bad = [s for s, c in coverage_by_symbol.items() if not c.get("complete")]
    if bad:
        return None, None, bad
    start = max(c["decision_start"] for c in coverage_by_symbol.values())
    end = min(c["decision_end"] for c in coverage_by_symbol.values())
    if end < start:
        return None, None, list(coverage_by_symbol)
    return start, end, []


async def _load_inputs(session, symbol: str) -> HistoricalInputs:
    c4 = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "240", limit=5000)]
    c1 = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "60", limit=5000)]
    c15 = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "15", limit=5000)]
    c1m = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "1", limit=20000)]
    trades = [rs.trade_agg_dict(x) for x in await qr.get_recent_trade_aggregates(session, symbol, 60, limit=20000)]
    liq = [rs.liq_agg_dict(x) for x in await qr.get_recent_liquidation_aggregates(session, symbol, 60, limit=20000)]
    ob = [rs.orderbook_dict(x) for x in await qr.get_recent_orderbook_snapshots(session, symbol, limit=20000)]
    deriv = [rs.derivatives_dict(x) for x in await qr.get_recent_derivatives_snapshots(session, symbol, limit=10000)]
    return HistoricalInputs(c4, c1, c15, c1m, trades, liq, ob, deriv)


def _future_1m(data: HistoricalInputs, cutoff: dt.datetime) -> list[dict]:
    return [dict(c) for c in data.candles_1m if c["open_time"] >= cutoff]


def _trade_row(result, mode: str, shadow=None) -> dict[str, Any]:
    order = result.order
    row = {
        "execution_mode": mode,
        "variant": result.signal.variant.value,
        "symbol": result.signal.symbol,
        "decision_time": result.signal.decision_time,
        "setup": result.signal.setup_name,
        "direction": result.signal.direction,
        "status": order["status"],
        "filled": bool(order.get("entry_time")),
        "entry_time": order.get("entry_time"),
        "entry_price": order.get("avg_entry_price"),
        "production_pnl_as_is": result.production_pnl_as_is,
        "corrected_net_pnl": result.corrected_net_pnl,
        "r_production": result.r_multiple_production,
        "r_corrected": result.r_multiple_corrected,
        "fees_paid": order.get("fees_paid"),
        "entry_fee_omitted_by_production_pnl": result.entry_fee_omitted_by_production_pnl,
        "slippage_cost": order.get("slippage_cost"),
        "funding_paid": order.get("funding_paid"),
        "remaining_size": order.get("remaining_size"),
        "tv_shadow_status": None,
        "tv_shadow_score": None,
        "tv_shadow_confidence": None,
        "tv_history_kind": None,
        "tv_can_affect_decision": None,
    }
    if shadow is not None:
        row.update({
            "tv_shadow_status": shadow.status,
            "tv_shadow_score": shadow.score,
            "tv_shadow_confidence": shadow.confidence,
            "tv_history_kind": shadow.history_kind,
            "tv_can_affect_decision": shadow.can_affect_decision,
        })
    return row


def _right_censored(observations) -> int:
    return sum(1 for x in observations if x.filled and x.r_multiple is None)


def _shadow_counterfactual(pairs) -> dict[str, Any]:
    baseline = [obs for _, obs in pairs]
    drop_contradicted = [obs for status, obs in pairs if status != "CONTRADICTED"]
    confirmed_or_weak = [obs for status, obs in pairs if status in {"CONFIRMED", "WEAK_CONFIRMATION"}]
    return {
        "BASELINE_ALL_RESEARCH_V2": summarize_observations(baseline),
        "HYPOTHETICAL_DROP_TV_CONTRADICTED": summarize_observations(drop_contradicted),
        "HYPOTHETICAL_KEEP_TV_CONFIRMED_OR_WEAK": summarize_observations(confirmed_or_weak),
        "predeclared_before_outcome_review": True,
        "tv_shadow_had_zero_effect_on_original_decisions": True,
    }


async def run(args) -> dict[str, Any]:
    settings = get_trading_settings()
    requested_symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] if args.symbols else list(settings.symbols)
    unknown = sorted(set(requested_symbols) - set(settings.symbols))
    if unknown:
        raise ValueError(f"symbols not in current TRADING_SYMBOLS: {unknown}")
    symbols = (["BTCUSDT"] if "BTCUSDT" in requested_symbols else []) + [x for x in requested_symbols if x != "BTCUSDT"]

    async with session_scope() as session:
        data_by_symbol = {s: await _load_inputs(session, s) for s in symbols}
    coverage = {s: coverage_from_inputs(data) for s, data in data_by_symbol.items()}
    exact_start, exact_end, bad_symbols = global_exact_window(coverage)
    if bad_symbols or exact_start is None or exact_end is None:
        payload = {
            "status": "INSUFFICIENT_EXACT_LOCAL_COVERAGE",
            "bad_symbols": bad_symbols,
            "coverage": coverage,
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "guarded_coverage.json").write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
        return payload

    req_start = dt.datetime.fromisoformat(args.start) if args.start else exact_start
    req_end = dt.datetime.fromisoformat(args.end) if args.end else exact_end
    if req_start.tzinfo is None or req_end.tzinfo is None:
        raise ValueError("--start/--end must be timezone-aware ISO timestamps")
    outside = req_start < exact_start or req_end > exact_end
    if outside and not args.auto_clamp:
        return {
            "status": "REQUEST_OUTSIDE_EXACT_OVERLAP",
            "requested": {"start": req_start, "end": req_end},
            "exact_common_window": {"start": exact_start, "end": exact_end},
            "hint": "rerun with --auto-clamp or choose a window inside exact_common_window",
            "coverage": coverage,
        }
    start = max(req_start, exact_start) if args.auto_clamp else req_start
    end = min(req_end, exact_end) if args.auto_clamp else req_end
    cutoffs = decision_times(start, end)
    if not cutoffs:
        return {"status": "NO_DECISION_TIMES_IN_WINDOW", "window": {"start": start, "end": end}}

    primary_obs = defaultdict(list)
    sensitivity_obs = defaultdict(list)
    primary_trade_rows: list[dict] = []
    sensitivity_trade_rows: list[dict] = []
    decision_rows: list[dict] = []
    pair_counts = Counter()
    family_counts = defaultdict(Counter)
    as_is_vs_fixed_counts = Counter()
    research_state = {s: None for s in symbols}
    shadow_counts = Counter()
    shadow_observations = defaultdict(list)
    shadow_pairs = []

    for cutoff in cutoffs:
        btc_regime = None
        research_chains: dict[str, dict] = {}
        for symbol in symbols:
            chain = build_research_v2_chain(
                symbol,
                data_by_symbol[symbol],
                cutoff,
                btc_regime=btc_regime,
                previous_state=research_state[symbol],
            )
            research_state[symbol] = chain.get("_state")
            research_chains[symbol] = chain
            if symbol == "BTCUSDT":
                btc_regime = chain["regime"].get("primary_regime")

        for symbol in symbols:
            data = data_by_symbol[symbol]
            legacy_as_is = neutral_signals_from_legacy(build_legacy_chain(symbol, data, cutoff, fixed_tv=False))
            legacy_fixed = neutral_signals_from_legacy(build_legacy_chain(symbol, data, cutoff, fixed_tv=True))
            research = neutral_signals_from_research(research_chains[symbol], approved_only=True)
            research_windows = windows_as_of(data, cutoff)
            research_shadow = {id(s): evaluate_tv_shadow(s, research_windows) for s in research}

            pair_category = signal_set_category(legacy_fixed, research)
            pair_counts[pair_category] += 1
            for family, category in paired_setup_families(legacy_fixed, research).items():
                family_counts[family][category] += 1
            as_is_vs_fixed_counts[signal_set_category(legacy_as_is, legacy_fixed)] += 1

            groups = {
                Variant.LEGACY_AS_IS.value: legacy_as_is,
                Variant.LEGACY_FIXED_TV.value: legacy_fixed,
                Variant.RESEARCH_V2.value: research,
            }
            for variant_name, signals in groups.items():
                for signal in signals:
                    future = _future_1m(data, cutoff)
                    shadow = research_shadow.get(id(signal)) if variant_name == Variant.RESEARCH_V2.value else None
                    primary = simulate_neutral_signal(
                        signal,
                        future,
                        target_schedule_policy=TargetSchedulePolicy.NORMALIZE_AVAILABLE_TARGETS,
                        intrabar_policy=EntryExitIntrabarPolicy.NEXT_CANDLE,
                    )
                    sensitivity = simulate_neutral_signal(
                        signal,
                        future,
                        target_schedule_policy=TargetSchedulePolicy.PRODUCTION_AS_IS,
                        intrabar_policy=EntryExitIntrabarPolicy.PRODUCTION_SAME_WINDOW,
                    )
                    primary_obs[variant_name].append(primary.observation)
                    sensitivity_obs[variant_name].append(sensitivity.observation)
                    primary_trade_rows.append(_trade_row(primary, "PRIMARY_NORMALIZED", shadow))
                    sensitivity_trade_rows.append(_trade_row(sensitivity, "W5_PRODUCTION_AS_IS_SENSITIVITY", shadow))
                    if shadow is not None:
                        shadow_counts[shadow.status] += 1
                        shadow_observations[shadow.status].append(primary.observation)
                        shadow_pairs.append((shadow.status, primary.observation))

            decision_rows.append({
                "cutoff": cutoff,
                "symbol": symbol,
                "legacy_as_is": [{"setup": s.setup_name, "direction": s.direction} for s in legacy_as_is],
                "legacy_fixed_tv": [{"setup": s.setup_name, "direction": s.direction} for s in legacy_fixed],
                "research_v2": [
                    {
                        "setup": s.setup_name,
                        "direction": s.direction,
                        "tv_shadow": research_shadow[id(s)].to_dict(),
                    }
                    for s in research
                ],
                "legacy_fixed_vs_research_category": pair_category,
                "matched_families": paired_setup_families(legacy_fixed, research),
                "research_regime": research_chains[symbol]["regime"].get("primary_regime"),
                "research_data_quality": research_chains[symbol]["data_quality"].get("data_quality_score"),
            })

    variants = (Variant.LEGACY_AS_IS, Variant.LEGACY_FIXED_TV, Variant.RESEARCH_V2)
    primary_summary = {v.value: summarize_observations(primary_obs[v.value]) for v in variants}
    sensitivity_summary = {v.value: summarize_observations(sensitivity_obs[v.value]) for v in variants}
    for v in variants:
        primary_summary[v.value]["right_censored_filled"] = _right_censored(primary_obs[v.value])
        sensitivity_summary[v.value]["right_censored_filled"] = _right_censored(sensitivity_obs[v.value])

    tv_shadow_outcomes = {
        status: summarize_observations(rows) for status, rows in sorted(shadow_observations.items())
    }
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "run_kind": "GUARDED_LOCAL_OVERLAP",
        "status": "COMPLETE_LOCAL_SMOKE",
        "final_statistical_verdict_allowed": False,
        "read_only": True,
        "selected_symbols": symbols,
        "requested_window": {"start": req_start, "end": req_end},
        "exact_common_window": {"start": exact_start, "end": exact_end},
        "used_window": {"start": start, "end": end},
        "decision_points_per_symbol": len(cutoffs),
        "coverage": coverage,
        "primary_execution": {
            "risk_pct": "1.0",
            "target_schedule": TargetSchedulePolicy.NORMALIZE_AVAILABLE_TARGETS.value,
            "intrabar_policy": EntryExitIntrabarPolicy.NEXT_CANDLE.value,
            "primary_pnl": "corrected_net_pnl",
            "summary": primary_summary,
        },
        "production_as_is_execution_sensitivity": {
            "target_schedule": TargetSchedulePolicy.PRODUCTION_AS_IS.value,
            "intrabar_policy": EntryExitIntrabarPolicy.PRODUCTION_SAME_WINDOW.value,
            "summary": sensitivity_summary,
        },
        "paired_legacy_fixed_vs_research": dict(pair_counts),
        "matched_family_counts": {family: dict(counts) for family, counts in family_counts.items()},
        "legacy_as_is_vs_fixed_signal_delta": dict(as_is_vs_fixed_counts),
        "research_v2_tv_shadow": {
            "can_affect_decision": False,
            "history_kind": "TV_RECONSTRUCTED_CLASSIC_TA_V1",
            "counts": dict(shadow_counts),
            "outcomes_by_status": tv_shadow_outcomes,
            "predeclared_counterfactual_filters": _shadow_counterfactual(shadow_pairs),
        },
        "w5_known_quirks": {
            "two_target_residual_fraction_production": str(production_two_target_residual_fraction()),
            "entry_fee_omitted_from_production_realized_pnl": True,
            "fill_candle_can_be_reused_for_exit_in_production_assembly": True,
            "repeated_partial_fill_overwrite_reproducer": True,
        },
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "guarded_smoke_summary.json").write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
    (OUT / "guarded_coverage.json").write_text(json.dumps(_jsonable(coverage), indent=2), encoding="utf-8")
    with (OUT / "guarded_decisions.jsonl").open("w", encoding="utf-8") as f:
        for row in decision_rows:
            f.write(json.dumps(_jsonable(row)) + "\n")

    all_trade_rows = primary_trade_rows + sensitivity_trade_rows
    if all_trade_rows:
        path = OUT / "guarded_trades.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_trade_rows[0].keys()))
            writer.writeheader()
            writer.writerows([{k: _jsonable(v) for k, v in row.items()} for row in all_trade_rows])
    return payload


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="timezone-aware ISO; default exact overlap start")
    p.add_argument("--end", help="timezone-aware ISO; default exact overlap end")
    p.add_argument("--symbols", help="comma-separated; default current TRADING_SYMBOLS")
    p.add_argument("--auto-clamp", action="store_true", help="clamp requested range to exact common overlap")
    return p.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(asyncio.run(run(parse_args()))), indent=2))
