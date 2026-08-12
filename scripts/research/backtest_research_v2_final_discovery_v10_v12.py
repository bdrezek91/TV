"""Final pre-registered discovery batch: V10-V12 versus frozen Mean Reversion.

This is a read-only 30d discovery runner. It evaluates three new hypotheses
without changing production Research V2, W1/W3/W4/W5, or the frozen
mean_reversion detector:
- V10 TRADE_FLOW_IMPULSE_CONTINUATION
- V11 BASIS_PREMIUM_DISLOCATION
- V12 VOLUME_WEIGHTED_TSMOM

Each candidate is tested alone and paired only with frozen Mean Reversion.
No V10+V11+V12 combination is tested on the discovery window.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_discovery_v10_v12 import (
    V10_FLOW_WINDOW_MINUTES,
    V10_IMBALANCE_15_MIN,
    V10_IMBALANCE_60_MIN,
    V10_MAX_STOP_ATR,
    V10_MIN_COVERED_MINUTES,
    V10_PRICE_MOVE_MAX_ATR,
    V10_PRICE_MOVE_MIN_ATR,
    V10_RECENT_MINUTES,
    V10_VOLUME_ACCELERATION_MIN,
    V11_BASIS_Z_MIN,
    V11_LOOKBACK_HOURS,
    V11_MAX_DERIVATIVE_AGE_MINUTES,
    V11_MAX_STOP_ATR,
    V11_MIN_DERIVATIVE_ROWS,
    V11_MIN_FUNDING_ABS,
    V11_TRIGGER_BODY_MIN_ATR,
    V12_BASELINE_15M_CANDLES,
    V12_MAX_STOP_ATR,
    V12_MIN_DIRECTIONAL_CANDLES,
    V12_MIN_VOLUME_EXPANSION,
    V12_MOVE_MAX_ATR,
    V12_MOVE_MIN_ATR,
    V12_RECENT_15M_CANDLES,
    detect_basis_dislocation_reversal,
    detect_trade_flow_impulse_continuation,
    detect_volume_weighted_tsmom,
    evaluate_discovery_setup,
)
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
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
CANDIDATES: dict[str, tuple[str, Callable]] = {
    "V10": ("trade_flow_impulse_continuation", detect_trade_flow_impulse_continuation),
    "V11": ("basis_premium_dislocation_reversal", detect_basis_dislocation_reversal),
    "V12": ("volume_weighted_tsmom", detect_volume_weighted_tsmom),
}
VARIANTS = {
    "MEAN_REVERSION_ONLY": {"mean_reversion"},
    "V10_TRADE_FLOW_ONLY": {"trade_flow_impulse_continuation"},
    "MEAN_REVERSION_PLUS_V10": {"mean_reversion", "trade_flow_impulse_continuation"},
    "V11_BASIS_DISLOCATION_ONLY": {"basis_premium_dislocation_reversal"},
    "MEAN_REVERSION_PLUS_V11": {"mean_reversion", "basis_premium_dislocation_reversal"},
    "V12_VOLUME_WEIGHTED_TSMOM_ONLY": {"volume_weighted_tsmom"},
    "MEAN_REVERSION_PLUS_V12": {"mean_reversion", "volume_weighted_tsmom"},
}


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
    selected = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] if symbols_arg else list(successful)
    missing = [s for s in selected if s not in successful]
    if missing:
        raise ValueError(f"missing successful cache for: {missing}")
    return selected, successful


def _future_candles(candles: list[dict], timestamps: list[dt.datetime], cutoff: dt.datetime) -> list[dict]:
    return candles[bisect.bisect_left(timestamps, cutoff):]


def _by_setup(rows: list[TradeObservation]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for row in rows:
        grouped[row.setup_name].append(row)
    return {name: summarize_observations(items) for name, items in sorted(grouped.items())}


def _lifecycle_variant(events: list[dict[str, Any]], allowed: set[str], end: dt.datetime) -> dict[str, Any]:
    eligible = [event for event in events if event["setup"] in allowed]
    conflict_keys: set[tuple[str, dt.datetime]] = set()
    if len(allowed) > 1:
        grouped: dict[tuple[str, dt.datetime], set[str]] = defaultdict(set)
        for event in eligible:
            grouped[(event["symbol"], event["decision_time"])].add(event["setup"])
        conflict_keys = {key for key, setups in grouped.items() if len(setups) > 1}
        eligible = [e for e in eligible if (e["symbol"], e["decision_time"]) not in conflict_keys]

    gate = SingleSymbolLifecycleGate()
    submitted: list[dict[str, Any]] = []
    suppressed = Counter()
    for event in eligible:
        if not gate.can_submit(event["symbol"], event["decision_time"]):
            suppressed[event["setup"]] += 1
            continue
        submitted.append(event)
        gate.occupy_from_order(event["symbol"], event["order"], fallback_end=end)

    rows = [event["observation"] for event in submitted]
    return {
        "lifecycle": summarize_observations(rows),
        "by_setup": _by_setup(rows),
        "submitted_by_setup": dict(Counter(event["setup"] for event in submitted)),
        "suppressed_busy_by_setup": dict(suppressed),
        "same_scan_cross_setup_conflicts_excluded": len(conflict_keys),
        "gate": gate.diagnostics(),
    }


def _run_schedule(
    schedule_name: str,
    hours: tuple[int, ...],
    requested: list[str],
    successful: dict,
    start: dt.datetime,
    end: dt.datetime,
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

    events: list[dict[str, Any]] = []
    candidate_diagnostics = {
        key: {
            "active_setup": 0,
            "approved_signal": 0,
            "w3": Counter(),
            "w4": Counter(),
            "by_regime": Counter(),
            "by_direction": Counter(),
        }
        for key in CANDIDATES
    }

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
            chain["_trade_flow_trades_1m"] = indexes[symbol].trailing_trades(cutoff, V10_FLOW_WINDOW_MINUTES)
            chain["_derivatives_24h"] = indexes[symbol].deriv.trailing(cutoff, 288)
            chains[symbol] = chain
            if symbol == "BTCUSDT":
                btc_regime = chain["regime"].get("primary_regime")

        for symbol in requested:
            chain = chains[symbol]
            future = _future_candles(candles[symbol], candle_times[symbol], cutoff)

            for signal in neutral_signals_from_research(chain, approved_only=True):
                if signal.setup_name != "mean_reversion":
                    continue
                result = simulate_neutral_signal(signal, future)
                events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "setup": "mean_reversion",
                    "observation": result.observation,
                    "order": result.order,
                })

            for key, (setup_name, detector) in CANDIDATES.items():
                evaluated = evaluate_discovery_setup(chain, detector, setup_name, btc_regime=btc_regime)
                setup = evaluated["setup"]
                direction = setup.get("direction")
                diag = candidate_diagnostics[key]
                if direction in {"LONG", "SHORT"}:
                    diag["active_setup"] += 1
                    diag["by_regime"][str(chain["regime"].get("primary_regime"))] += 1
                    diag["by_direction"][str(direction)] += 1
                    confirmation = evaluated.get("confirmation") or {}
                    risk = evaluated.get("risk") or {}
                    diag["w3"][str(confirmation.get("status"))] += 1
                    diag["w4"][str(risk.get("decision"))] += 1

                signal = evaluated.get("signal")
                if signal is None:
                    continue
                diag["approved_signal"] += 1
                result = simulate_neutral_signal(signal, future)
                events.append({
                    "symbol": symbol,
                    "decision_time": signal.decision_time,
                    "setup": setup_name,
                    "observation": result.observation,
                    "order": result.order,
                })

    diagnostics_out = {}
    for key, diag in candidate_diagnostics.items():
        diagnostics_out[key] = {
            "active_setup": diag["active_setup"],
            "approved_signal": diag["approved_signal"],
            "w3": dict(diag["w3"]),
            "w4": dict(diag["w4"]),
            "by_regime": dict(diag["by_regime"]),
            "by_direction": dict(diag["by_direction"]),
        }

    return {
        "schedule": {
            "name": schedule_name,
            "hours": list(hours),
            "cutoffs_per_symbol": len(cutoffs),
            "symbols": len(requested),
            "total_symbol_decision_points": len(cutoffs) * len(requested),
        },
        "event_pool_by_setup": dict(Counter(event["setup"] for event in events)),
        "candidate_diagnostics": diagnostics_out,
        "variants": {name: _lifecycle_variant(events, allowed, end) for name, allowed in VARIANTS.items()},
        "interpretation_contract": (
            "V10-V12 are pre-registered final-discovery hypotheses. Frozen Mean Reversion is unchanged. "
            "Each new candidate is compared alone and paired only with Mean Reversion; no V10+V11+V12 "
            "combination is searched on the discovery window. FULL_24H_1H vs 2H is sensitivity, not an "
            "independent statistical replication, because W1 hysteresis is tick-based."
        ),
    }


def _pre_registration() -> dict[str, Any]:
    return {
        "frozen_before_outcomes": True,
        "discovery_window_reuse_warning": (
            "This 30d window has already been inspected by V3-V9. V10-V12 are the final predeclared batch; "
            "no threshold tuning is permitted after their outcomes are observed."
        ),
        "V10_TRADE_FLOW_IMPULSE_CONTINUATION": {
            "thesis": "continue aggressive taker flow only when price direction and rising OI indicate new positions",
            "flow_window_minutes": V10_FLOW_WINDOW_MINUTES,
            "recent_flow_minutes": V10_RECENT_MINUTES,
            "minimum_covered_minutes": V10_MIN_COVERED_MINUTES,
            "minimum_60m_taker_imbalance": V10_IMBALANCE_60_MIN,
            "minimum_15m_taker_imbalance": V10_IMBALANCE_15_MIN,
            "minimum_recent_vs_prior_volume_acceleration": V10_VOLUME_ACCELERATION_MIN,
            "one_hour_price_move_atr_band": [V10_PRICE_MOVE_MIN_ATR, V10_PRICE_MOVE_MAX_ATR],
            "required_price_oi_relation": ["PRICE_UP_OI_UP for LONG", "PRICE_DOWN_OI_UP for SHORT"],
            "maximum_stop_risk_atr": V10_MAX_STOP_ATR,
        },
        "V11_BASIS_PREMIUM_DISLOCATION": {
            "thesis": "fade statistically extreme perp/index basis when funding crowds the same side and price triggers against it",
            "basis_lookback_hours": V11_LOOKBACK_HOURS,
            "minimum_derivative_rows": V11_MIN_DERIVATIVE_ROWS,
            "minimum_abs_basis_zscore": V11_BASIS_Z_MIN,
            "minimum_abs_funding_rate": V11_MIN_FUNDING_ABS,
            "maximum_latest_derivative_age_minutes": V11_MAX_DERIVATIVE_AGE_MINUTES,
            "minimum_counter_crowd_trigger_body_atr": V11_TRIGGER_BODY_MIN_ATR,
            "maximum_stop_risk_atr": V11_MAX_STOP_ATR,
            "long_short_ratio": "diagnostic only; not a gate because timestamp interval semantics remain source-ambiguous",
        },
        "V12_VOLUME_WEIGHTED_TSMOM": {
            "thesis": "time-series momentum only when recent 2h volume expands and volume-weighted returns agree with 4h direction",
            "recent_15m_candles": V12_RECENT_15M_CANDLES,
            "baseline_15m_candles": V12_BASELINE_15M_CANDLES,
            "minimum_recent_vs_prior_2h_volume_expansion": V12_MIN_VOLUME_EXPANSION,
            "two_hour_move_atr_band": [V12_MOVE_MIN_ATR, V12_MOVE_MAX_ATR],
            "minimum_directional_15m_candles": V12_MIN_DIRECTIONAL_CANDLES,
            "requires_volume_weighted_return_same_sign": True,
            "requires_4h_time_series_momentum_same_sign": True,
            "maximum_stop_risk_atr": V12_MAX_STOP_ATR,
        },
        "holdout_eligibility_rule": {
            "positive_expectancy_schedules_min": 2,
            "profit_factor_at_least_1_15_schedules_min": 2,
            "catastrophic_schedule_floor_expectancy_r": "-0.20",
            "sample_interpretation": {
                "<30": "INSUFFICIENT",
                "30-99": "PRELIMINARY",
                "100-299": "MODERATE",
                "300+": "STRONGER",
            },
            "note": (
                "Eligibility is not proof of edge. Any survivor must be frozen and evaluated on a truly "
                "non-overlapping untouched holdout. Combined MR+candidate results are diagnostic and must "
                "not be threshold-tuned on this discovery window."
            ),
        },
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
        "candidate_kind": "RESEARCH_V2_FINAL_DISCOVERY_V10_V12",
        "status": "RESEARCH_ONLY_NOT_PROMOTED",
        "read_only": True,
        "evaluation_window": {"start": start, "end": end},
        "requested_symbols": requested,
        "pre_registration": _pre_registration(),
        "results": {
            name: _run_schedule(name, hours, requested, successful, start, end)
            for name, hours in schedules.items()
        },
        "guards": [
            "production Research V2 unchanged",
            "frozen Mean Reversion logic unchanged",
            "V10-V12 thresholds fixed before observing V10-V12 outcomes",
            "V10 dedicated 60m taker-flow history does not alter standard W1/W3 30m inputs",
            "V11 dedicated 24h derivative history uses point-in-time snapshots only",
            "V11 long/short ratio is diagnostic only because source interval timestamp semantics remain ambiguous",
            "V12 uses closed candles only",
            "W3/W4 thresholds unchanged",
            "W5 fees/slippage/funding unchanged",
            "no historical orderbook/liquidation values fabricated",
            "same-symbol same-scan conflicts are excluded in MR+candidate variants",
            "no V10+V11+V12 combination is searched on discovery data",
            "FULL_24H_1H and FULL_24H_2H are sensitivity runs, not independent replications",
        ],
    }
    out_dir = cache_root.parent if cache_root.name == "cache" else cache_root
    write_json_cache(out_dir / "research_v2_final_discovery_v10_v12.json", payload)
    return payload


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--schedule", choices=sorted(SCHEDULES))
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(_jsonable(main(parse_args())), indent=2))
