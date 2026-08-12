#!/usr/bin/env python3
"""Download/cache official Bybit history for BACKTEST_COMPARISON_V1.

READ-ONLY relative to production. All files go under the research cache root;
no Postgres writes, no paper-state reads/writes, no order endpoints.

Safe default: 30 evaluation days + 10 warm-up days for five smoke symbols.
Use --all-symbols deliberately for the full configured universe.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from tradingview_mcp.core.backtest.extended_history_downloader_v1 import (
    DownloadConfig,
    OfficialBybitDownloader,
)
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, write_json_cache
from tradingview_mcp.core.config.trading_settings import get_trading_settings


DEFAULT_SMOKE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT")
UTC = dt.timezone.utc


def _completed_utc_day_end(now: dt.datetime | None = None) -> dt.datetime:
    """Last instant of the most recent fully completed UTC day."""
    now = now or dt.datetime.now(UTC)
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - dt.timedelta(microseconds=1)


def resolve_window(days: int, warmup_days: int, end: dt.datetime | None = None) -> dict:
    if days < 1 or days > 365:
        raise ValueError("--days must be 1..365")
    if warmup_days < 9 or warmup_days > 60:
        raise ValueError("--warmup-days must be 9..60 (4h EMA50 needs >8 days)")
    evaluation_end = end or _completed_utc_day_end()
    if evaluation_end.tzinfo is None:
        raise ValueError("end must be timezone-aware")
    evaluation_end = evaluation_end.astimezone(UTC)
    evaluation_start = evaluation_end - dt.timedelta(days=days) + dt.timedelta(microseconds=1)
    download_start = evaluation_start - dt.timedelta(days=warmup_days)
    return {
        "evaluation_start": evaluation_start,
        "evaluation_end": evaluation_end,
        "download_start": download_start,
        "download_end": evaluation_end,
        "evaluation_days": days,
        "warmup_days": warmup_days,
    }


def resolve_symbols(args) -> list[str]:
    configured = list(get_trading_settings().symbols)
    configured_set = set(configured)
    if args.all_symbols:
        selected = configured
    elif args.symbols:
        selected = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    else:
        selected = [s for s in DEFAULT_SMOKE_SYMBOLS if s in configured_set]
    unknown = sorted(set(selected) - configured_set)
    if unknown:
        raise ValueError(f"symbols not present in current TRADING_SYMBOLS: {unknown}")
    if "BTCUSDT" in selected:
        selected = ["BTCUSDT"] + [s for s in selected if s != "BTCUSDT"]
    return selected


async def run(args) -> dict:
    symbols = resolve_symbols(args)
    end_override = dt.datetime.fromisoformat(args.end) if args.end else None
    window = resolve_window(args.days, args.warmup_days, end_override)
    cache_root = Path(args.cache_root)
    config = DownloadConfig(
        cache_root=cache_root,
        timeout_seconds=args.timeout,
        max_retries=args.retries,
        keep_raw_trades=args.keep_raw_trades,
        request_pause_seconds=args.request_pause,
    )
    results = {}
    async with OfficialBybitDownloader(config) as downloader:
        for symbol in symbols:
            try:
                manifest = await downloader.download_symbol(
                    symbol, window["download_start"], window["download_end"]
                )
                results[symbol] = {"status": "OK", "manifest": manifest}
            except Exception as exc:  # continue other symbols, preserve failure
                results[symbol] = {"status": "ERROR", "error": repr(exc)}

    summary = {
        "research_contract": "BACKTEST_COMPARISON_V1",
        "operation": "EXTENDED_HISTORY_BACKFILL",
        "read_only_production": True,
        "symbols": symbols,
        "window": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in window.items()},
        "cache_root": str(cache_root),
        "keep_raw_trades": args.keep_raw_trades,
        "results": results,
        "successful": sum(1 for r in results.values() if r["status"] == "OK"),
        "failed": sum(1 for r in results.values() if r["status"] == "ERROR"),
        "warning": (
            "Extended manifests remain DEGRADED until validated historical "
            "orderbook/liquidation adapters are available; do not call this an exact legacy backtest."
        ),
    }
    write_json_cache(cache_root / "backfill_summary.json", summary)
    return summary


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30, help="evaluation horizon; 30 or 90 are typical")
    p.add_argument("--warmup-days", type=int, default=10, help="extra history before evaluation start")
    p.add_argument("--end", help="timezone-aware ISO end; default last completed UTC day")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--symbols", help="comma-separated subset of configured TRADING_SYMBOLS")
    group.add_argument("--all-symbols", action="store_true", help="download all currently configured symbols")
    p.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    p.add_argument("--keep-raw-trades", action="store_true", help="keep daily csv.gz after 1m aggregation")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--request-pause", type=float, default=0.05, help="polite delay between page/archive requests")
    return p.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, default=str))
