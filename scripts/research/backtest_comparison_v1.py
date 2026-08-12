"""BACKTEST_COMPARISON_V1 research CLI.

Read-only tooling for comparing legacy TradingView+Bybit with Research V2.
Nothing here loads/saves the live W5 paper state or writes production DB rows.

Useful VPS commands:

    python scripts/research/backtest_comparison_v1.py coverage
    python scripts/research/backtest_comparison_v1.py audit-tv
    python scripts/research/backtest_comparison_v1.py decision-clock \
        --start 2026-08-01T00:00:00+00:00 --end 2026-08-10T23:59:00+00:00
    python scripts/research/backtest_comparison_v1.py smoke \
        --start 2026-08-09T00:00:00+00:00 --end 2026-08-10T12:00:00+00:00 \
        --symbols BTCUSDT,ETHUSDT,SOLUSDT

`smoke` is intentionally labelled SMOKE_LOCAL_OVERLAP. It validates mechanics
on the data already retained by Postgres; it is not the final 30/90-day result.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingview_mcp.core.analysis import tradingview_context as legacy_tv
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_v1 import (
    HistoricalInputs,
    build_legacy_chain,
    build_research_v2_chain,
    neutral_signals_from_legacy,
    neutral_signals_from_research,
)
from tradingview_mcp.core.backtest.comparison_v1 import (
    TvHistoryKind,
    Variant,
    decision_times,
    legacy_parser_mismatch,
    parse_current_tv_response_fixed,
    summarize_observations,
)
from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.repositories import query_repository as qr
from tradingview_mcp.core.database.session import session_scope
from tradingview_mcp.core.services import regime_service_v2 as rs

OUT = Path("/app/artifacts/research/backtest_comparison")


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, Variant):
        return v.value
    if is_dataclass(v):
        return _jsonable(asdict(v))
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _span(rows: list[Any], attr: str) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "earliest": None, "latest": None}
    vals = [getattr(r, attr) for r in rows]
    return {"count": len(rows), "earliest": _iso(min(vals)), "latest": _iso(max(vals))}


async def _coverage_for_symbol(session, symbol: str) -> dict[str, Any]:
    c4 = await qr.get_recent_candles(session, symbol, "240", limit=5000)
    c1 = await qr.get_recent_candles(session, symbol, "60", limit=5000)
    c15 = await qr.get_recent_candles(session, symbol, "15", limit=5000)
    c1m = await qr.get_recent_candles(session, symbol, "1", limit=10000)
    trades = await qr.get_recent_trade_aggregates(session, symbol, 60, limit=10000)
    liq = await qr.get_recent_liquidation_aggregates(session, symbol, 60, limit=10000)
    ob = await qr.get_recent_orderbook_snapshots(session, symbol, limit=10000)
    deriv = await qr.get_recent_derivatives_snapshots(session, symbol, limit=10000)
    return {
        "4h": _span(c4, "open_time"),
        "1h": _span(c1, "open_time"),
        "15m": _span(c15, "open_time"),
        "candles_1m": _span(c1m, "open_time"),
        "trades_1m": _span(trades, "bucket_start"),
        "liquidations_1m": _span(liq, "bucket_start"),
        "orderbook": _span(ob, "source_timestamp"),
        "derivatives": _span(deriv, "source_timestamp"),
    }


def _intersection(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict overlap: every required source must be present."""
    missing = [i for i, s in enumerate(spans) if not s.get("earliest") or not s.get("latest")]
    if missing:
        return {"earliest": None, "latest": None, "hours": 0.0, "complete": False, "missing_source_indexes": missing}
    starts = [dt.datetime.fromisoformat(s["earliest"]) for s in spans]
    ends = [dt.datetime.fromisoformat(s["latest"]) for s in spans]
    start, end = max(starts), min(ends)
    if end < start:
        return {"earliest": None, "latest": None, "hours": 0.0, "complete": False, "missing_source_indexes": []}
    return {
        "earliest": start.isoformat(),
        "latest": end.isoformat(),
        "hours": round((end - start).total_seconds() / 3600, 3),
        "complete": True,
        "missing_source_indexes": [],
    }


async def cmd_coverage() -> dict[str, Any]:
    settings = get_trading_settings()
    results: dict[str, Any] = {}
    async with session_scope() as session:
        for symbol in settings.symbols:
            results[symbol] = await _coverage_for_symbol(session, symbol)

    required_names = [
        "4h", "1h", "15m", "candles_1m", "trades_1m",
        "liquidations_1m", "orderbook", "derivatives",
    ]
    for sources in results.values():
        overlap = _intersection([sources[name] for name in required_names])
        overlap["required_sources"] = required_names
        overlap["missing_sources"] = [required_names[i] for i in overlap.pop("missing_source_indexes")]
        sources["full_source_overlap"] = overlap

    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "read_only": True,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "data_coverage.json").write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
    return payload


async def cmd_audit_tv() -> dict[str, Any]:
    settings = get_trading_settings()
    rows: dict[str, Any] = {}
    for symbol in settings.symbols:
        try:
            raw = await asyncio.to_thread(legacy_tv._default_analyzer, symbol, "BYBIT")
            legacy_ctx = legacy_tv.parse_tradingview_response(symbol, raw)
            fixed_ctx = parse_current_tv_response_fixed(symbol, raw)
            rows[symbol] = {
                "schema_mismatch": legacy_parser_mismatch(raw),
                "legacy_as_is": {
                    "trend_15m": legacy_ctx.trend_15m,
                    "trend_1h": legacy_ctx.trend_1h,
                    "trend_4h": legacy_ctx.trend_4h,
                    "ema20": str(legacy_ctx.ema20) if legacy_ctx.ema20 is not None else None,
                    "ema50": str(legacy_ctx.ema50) if legacy_ctx.ema50 is not None else None,
                    "rsi": str(legacy_ctx.rsi) if legacy_ctx.rsi is not None else None,
                },
                "legacy_fixed_tv": {
                    "trend_15m": fixed_ctx.trend_15m,
                    "trend_1h": fixed_ctx.trend_1h,
                    "trend_4h": fixed_ctx.trend_4h,
                    "ema20": str(fixed_ctx.ema20) if fixed_ctx.ema20 is not None else None,
                    "ema50": str(fixed_ctx.ema50) if fixed_ctx.ema50 is not None else None,
                    "rsi": str(fixed_ctx.rsi) if fixed_ctx.rsi is not None else None,
                },
            }
        except Exception as exc:
            rows[symbol] = {"error": repr(exc)}

    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "tv_history_kind": TvHistoryKind.LIVE_CURRENT.value,
        "note": "Current live TV response audit only; not historical TradingView.",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tv_parser_audit.json").write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
    return payload


def cmd_decision_clock(start: str, end: str) -> dict[str, Any]:
    rows = decision_times(dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end))
    return {
        "timezone": "Europe/Warsaw",
        "local_hours": [7, 9, 11, 13, 15, 17, 19, 21],
        "decision_times_utc": [x.isoformat() for x in rows],
        "count": len(rows),
    }


async def _load_inputs(session, symbol: str) -> HistoricalInputs:
    c4 = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "240", limit=5000)]
    c1 = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "60", limit=5000)]
    c15 = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "15", limit=5000)]
    c1m = [rs.candle_dict(x) for x in await qr.get_recent_candles(session, symbol, "1", limit=10000)]
    trades = [rs.trade_agg_dict(x) for x in await qr.get_recent_trade_aggregates(session, symbol, 60, limit=10000)]
    liq = [rs.liq_agg_dict(x) for x in await qr.get_recent_liquidation_aggregates(session, symbol, 60, limit=10000)]
    ob = [rs.orderbook_dict(x) for x in await qr.get_recent_orderbook_snapshots(session, symbol, limit=10000)]
    deriv = [rs.derivatives_dict(x) for x in await qr.get_recent_derivatives_snapshots(session, symbol, limit=10000)]
    return HistoricalInputs(c4, c1, c15, c1m, trades, liq, ob, deriv)


def _execution_candles(data: HistoricalInputs, cutoff: dt.datetime) -> list[dict]:
    # A candle opening exactly at cutoff is future execution path, not a
    # decision input. simulate_neutral_signal applies the same cutoff again.
    return [dict(c) for c in data.candles_1m if c["open_time"] >= cutoff]


def _trade_row(result) -> dict[str, Any]:
    order = result.order
    return {
        "variant": result.signal.variant.value,
        "symbol": result.signal.symbol,
        "decision_time": result.signal.decision_time.isoformat(),
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
    }


async def cmd_smoke(start: str, end: str, symbols_arg: str | None) -> dict[str, Any]:
    start_dt, end_dt = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise ValueError("--start/--end must include timezone offsets")
    settings = get_trading_settings()
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] if symbols_arg else list(settings.symbols)
    if "BTCUSDT" in symbols:
        symbols = ["BTCUSDT"] + [s for s in symbols if s != "BTCUSDT"]
    cutoffs = decision_times(start_dt, end_dt)

    async with session_scope() as session:
        data_by_symbol = {s: await _load_inputs(session, s) for s in symbols}

    observations = defaultdict(list)
    trade_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    research_state_by_symbol: dict[str, dict | None] = {s: None for s in symbols}

    for cutoff in cutoffs:
        btc_regime = None
        research_chains: dict[str, dict] = {}
        # Research BTC must be evaluated first so alts receive the same BTC
        # context at this cutoff.
        for symbol in symbols:
            chain = build_research_v2_chain(
                symbol, data_by_symbol[symbol], cutoff,
                btc_regime=btc_regime,
                previous_state=research_state_by_symbol[symbol],
            )
            research_state_by_symbol[symbol] = chain.get("_state")
            research_chains[symbol] = chain
            if symbol == "BTCUSDT":
                btc_regime = chain["regime"].get("primary_regime")

        for symbol in symbols:
            data = data_by_symbol[symbol]
            chains = [
                build_legacy_chain(symbol, data, cutoff, fixed_tv=False),
                build_legacy_chain(symbol, data, cutoff, fixed_tv=True),
                research_chains[symbol],
            ]
            signals_by_variant: dict[str, list] = {}
            for chain in chains:
                if chain["variant"] in {Variant.LEGACY_AS_IS, Variant.LEGACY_FIXED_TV}:
                    signals = neutral_signals_from_legacy(chain)
                else:
                    signals = neutral_signals_from_research(chain, approved_only=True)
                signals_by_variant[chain["variant"].value] = signals
                for signal in signals:
                    result = simulate_neutral_signal(signal, _execution_candles(data, cutoff))
                    observations[signal.variant.value].append(result.observation)
                    trade_rows.append(_trade_row(result))

            decision_rows.append({
                "cutoff": cutoff,
                "symbol": symbol,
                "legacy_as_is_signals": len(signals_by_variant.get(Variant.LEGACY_AS_IS.value, [])),
                "legacy_fixed_tv_signals": len(signals_by_variant.get(Variant.LEGACY_FIXED_TV.value, [])),
                "research_v2_signals": len(signals_by_variant.get(Variant.RESEARCH_V2.value, [])),
                "research_regime": research_chains[symbol]["regime"].get("primary_regime"),
            })

    summary = {
        v.value: summarize_observations(observations.get(v.value, []))
        for v in (Variant.LEGACY_AS_IS, Variant.LEGACY_FIXED_TV, Variant.RESEARCH_V2)
    }
    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "run_kind": "SMOKE_LOCAL_OVERLAP",
        "final_statistical_verdict_allowed": False,
        "read_only": True,
        "start": start_dt,
        "end": end_dt,
        "symbols": symbols,
        "decision_points": len(cutoffs),
        "execution": {
            "common_fixed_risk_pct": "1.0",
            "target_schedule": "NORMALIZE_AVAILABLE_TARGETS",
            "entry_exit_intrabar": "NEXT_CANDLE",
            "pnl_primary": "corrected_net_pnl (production realized_pnl minus omitted entry fee)",
        },
        "summary": summary,
        "research_v2_tv": "PENDING_EXTERNAL_CONFIRMATION_HISTORY_ADAPTER",
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "smoke_summary.json").write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
    with (OUT / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for row in decision_rows:
            f.write(json.dumps(_jsonable(row)) + "\n")
    if trade_rows:
        with (OUT / "trades.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(trade_rows[0].keys()))
            writer.writeheader()
            writer.writerows([{k: _jsonable(v) for k, v in row.items()} for row in trade_rows])
    return payload


async def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("coverage")
    sub.add_parser("audit-tv")
    clock = sub.add_parser("decision-clock")
    clock.add_argument("--start", required=True)
    clock.add_argument("--end", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--start", required=True)
    smoke.add_argument("--end", required=True)
    smoke.add_argument("--symbols")
    args = p.parse_args()

    if args.command == "coverage":
        result = await cmd_coverage()
    elif args.command == "audit-tv":
        result = await cmd_audit_tv()
    elif args.command == "decision-clock":
        result = cmd_decision_clock(args.start, args.end)
    else:
        result = await cmd_smoke(args.start, args.end, args.symbols)
    print(json.dumps(_jsonable(result), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
