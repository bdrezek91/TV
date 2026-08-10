#!/usr/bin/env python3
"""BACKTEST_COMPARISON_V1 research CLI.

Phase-0/1 harness for the comparative study.  It is intentionally read-only:
no paper state is loaded/saved, no signal snapshots are appended, and no DB
writes are performed.

Useful VPS commands:

    python scripts/research/backtest_comparison_v1.py coverage
    python scripts/research/backtest_comparison_v1.py audit-tv
    python scripts/research/backtest_comparison_v1.py decision-clock \
        --start 2026-08-01T00:00:00+00:00 --end 2026-08-10T23:59:00+00:00

The eventual historical trade replay belongs in this same research namespace,
but must not be reported as complete until full point-in-time input coverage
and the common W5 execution adapter are wired and validated.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

from tradingview_mcp.core.analysis import tradingview_context as legacy_tv
from tradingview_mcp.core.backtest.comparison_v1 import (
    TvHistoryKind,
    decision_times,
    legacy_parser_mismatch,
    parse_current_tv_response_fixed,
)
from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.repositories import query_repository as qr
from tradingview_mcp.core.database.session import session_scope

OUT = Path("/app/artifacts/research/backtest_comparison")


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _span(rows: list[Any], attr: str) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "earliest": None, "latest": None}
    vals = [getattr(r, attr) for r in rows]
    return {"count": len(rows), "earliest": _iso(min(vals)), "latest": _iso(max(vals))}


async def _coverage_for_symbol(session, symbol: str) -> dict[str, Any]:
    # High limits are bounded by repository methods / actual retention. This is
    # an audit, not a backfill. It never writes to the production database.
    c4 = await qr.get_recent_candles(session, symbol, "240", limit=5000)
    c1 = await qr.get_recent_candles(session, symbol, "60", limit=5000)
    c15 = await qr.get_recent_candles(session, symbol, "15", limit=5000)
    trades = await qr.get_recent_trade_aggregates(session, symbol, 60, limit=10000)
    liq = await qr.get_recent_liquidation_aggregates(session, symbol, 60, limit=10000)
    ob = await qr.get_recent_orderbook_snapshots(session, symbol, limit=10000)
    deriv = await qr.get_recent_derivatives_snapshots(session, symbol, limit=10000)
    return {
        "4h": _span(c4, "open_time"),
        "1h": _span(c1, "open_time"),
        "15m": _span(c15, "open_time"),
        "trades_1m": _span(trades, "bucket_start"),
        "liquidations_1m": _span(liq, "bucket_start"),
        "orderbook": _span(ob, "source_timestamp"),
        "derivatives": _span(deriv, "source_timestamp"),
    }


def _intersection(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict overlap: every required source must be present.

    Missing orderbook/liquidations/etc. must never be silently ignored, because
    that would make an incomplete replay window look fully supported.
    """
    missing = [i for i, s in enumerate(spans) if not s.get("earliest") or not s.get("latest")]
    if missing:
        return {"earliest": None, "latest": None, "hours": 0.0, "complete": False, "missing_source_indexes": missing}
    starts = [dt.datetime.fromisoformat(s["earliest"]) for s in spans]
    ends = [dt.datetime.fromisoformat(s["latest"]) for s in spans]
    start, end = max(starts), min(ends)
    if end < start:
        return {"earliest": None, "latest": None, "hours": 0.0, "complete": False, "missing_source_indexes": []}
    hours = (end - start).total_seconds() / 3600
    return {
        "earliest": start.isoformat(),
        "latest": end.isoformat(),
        "hours": round(hours, 3),
        "complete": True,
        "missing_source_indexes": [],
    }


async def cmd_coverage() -> dict[str, Any]:
    settings = get_trading_settings()
    results: dict[str, Any] = {}
    async with session_scope() as session:
        for symbol in settings.symbols:
            results[symbol] = await _coverage_for_symbol(session, symbol)

    required_names = ["4h", "1h", "15m", "trades_1m", "liquidations_1m", "orderbook", "derivatives"]
    for symbol, sources in results.items():
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
    (OUT / "data_coverage.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
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
        except Exception as exc:  # audit must continue across symbols
            rows[symbol] = {"error": repr(exc)}

    payload = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "tv_history_kind": TvHistoryKind.LIVE_CURRENT.value,
        "note": "Current live TV response audit only; not historical TradingView.",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tv_parser_audit.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def cmd_decision_clock(start: str, end: str) -> dict[str, Any]:
    s = dt.datetime.fromisoformat(start)
    e = dt.datetime.fromisoformat(end)
    rows = decision_times(s, e)
    return {
        "timezone": "Europe/Warsaw",
        "local_hours": [7, 9, 11, 13, 15, 17, 19, 21],
        "decision_times_utc": [x.isoformat() for x in rows],
        "count": len(rows),
    }


async def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("coverage")
    sub.add_parser("audit-tv")
    clock = sub.add_parser("decision-clock")
    clock.add_argument("--start", required=True)
    clock.add_argument("--end", required=True)
    args = p.parse_args()

    if args.command == "coverage":
        result = await cmd_coverage()
    elif args.command == "audit-tv":
        result = await cmd_audit_tv()
    else:
        result = cmd_decision_clock(args.start, args.end)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
