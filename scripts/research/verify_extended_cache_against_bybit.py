#!/usr/bin/env python3
"""Spot-check extended research cache directly against official Bybit REST.

This is a read-only online verifier. For each selected symbol it chooses a
deterministic cached row (the middle row) from each REST-backed dataset, fetches
the same historical timestamp from api.bybit.com, and compares the raw Bybit
fields to the cached normalized values without reusing the cache parsers.

Public-trade archives are validated separately by the offline quality validator
through taker-volume reconciliation against official 1m klines, avoiding large
redownloads merely for spot-checking.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, OFFICIAL_API_BASE

UTC = dt.timezone.utc
VERIFY_VERSION = "BYBIT_RAW_REST_SPOTCHECK_V1"
REL_TOL = Decimal("0.00000001")
ABS_TOL = Decimal("0.00000001")


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out.astimezone(UTC)


def _ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _close(a: Any, b: Any) -> bool:
    aa, bb = _d(a), _d(b)
    return abs(aa - bb) <= max(ABS_TOL, REL_TOL * max(abs(aa), abs(bb), Decimal("1")))


def _payload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict[str, Any]]:
    payload = _payload(path)
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"cache rows empty/invalid: {path}")
    return [dict(row) for row in rows]


def _middle(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[len(rows) // 2]


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(OFFICIAL_API_BASE + path, params=params)
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("retCode", 0)) != 0:
        raise RuntimeError(f"Bybit retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}")
    return payload


def _find_list_row(payload: dict[str, Any], timestamp_ms: int, *, dict_timestamp_field: str | None = None) -> Any:
    rows = (payload.get("result") or {}).get("list") or []
    for row in rows:
        if isinstance(row, list):
            if row and int(row[0]) == timestamp_ms:
                return row
        elif isinstance(row, dict) and dict_timestamp_field:
            value = row.get(dict_timestamp_field)
            if value is not None and int(value) == timestamp_ms:
                return row
    return None


async def _verify_kline(client: httpx.AsyncClient, symbol: str, cache_dir: Path, *, name: str, path: str, interval: str, price_only: bool) -> dict[str, Any]:
    cached = _middle(_rows(cache_dir / f"{name}.json"))
    ts = _dt(cached["open_time"])
    payload = await _get(client, path, {
        "category": "linear", "symbol": symbol, "interval": interval,
        "start": _ms(ts), "end": _ms(ts), "limit": 2,
    })
    raw = _find_list_row(payload, _ms(ts))
    if raw is None:
        return {"passed": False, "timestamp": ts.isoformat(), "error": "raw Bybit row not found"}
    fields = {"open": 1, "high": 2, "low": 3, "close": 4}
    if not price_only:
        fields["volume"] = 5
        fields["turnover"] = 6
    mismatches = {}
    for field, idx in fields.items():
        cached_value = cached.get(field)
        raw_value = raw[idx] if len(raw) > idx else None
        if cached_value is None and raw_value is None:
            continue
        if cached_value is None or raw_value is None or not _close(cached_value, raw_value):
            mismatches[field] = {"cached": cached_value, "raw": raw_value}
    return {"passed": not mismatches, "timestamp": ts.isoformat(), "mismatches": mismatches}


async def _verify_oi(client: httpx.AsyncClient, symbol: str, cache_dir: Path) -> dict[str, Any]:
    cached = _middle(_rows(cache_dir / "open_interest_5m.json"))
    ts = _dt(cached["source_timestamp"])
    payload = await _get(client, "/v5/market/open-interest", {
        "category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 20,
        "startTime": _ms(ts - dt.timedelta(minutes=5)), "endTime": _ms(ts + dt.timedelta(minutes=5)),
    })
    raw = _find_list_row(payload, _ms(ts), dict_timestamp_field="timestamp")
    raw_value = raw.get("openInterest") if raw else None
    passed = raw_value is not None and _close(cached["open_interest"], raw_value)
    return {"passed": passed, "timestamp": ts.isoformat(), "cached": cached.get("open_interest"), "raw": raw_value}


async def _verify_ratio(client: httpx.AsyncClient, symbol: str, cache_dir: Path) -> dict[str, Any]:
    cached = _middle(_rows(cache_dir / "long_short_ratio_5m.json"))
    ts = _dt(cached["source_timestamp"])
    payload = await _get(client, "/v5/market/account-ratio", {
        "category": "linear", "symbol": symbol, "period": "5min", "limit": 20,
        "startTime": _ms(ts - dt.timedelta(minutes=5)), "endTime": _ms(ts + dt.timedelta(minutes=5)),
    })
    raw = _find_list_row(payload, _ms(ts), dict_timestamp_field="timestamp")
    mismatches = {}
    for cached_field, raw_field in (("buy_ratio", "buyRatio"), ("sell_ratio", "sellRatio")):
        raw_value = raw.get(raw_field) if raw else None
        if raw_value is None or not _close(cached[cached_field], raw_value):
            mismatches[cached_field] = {"cached": cached.get(cached_field), "raw": raw_value}
    return {"passed": not mismatches, "timestamp": ts.isoformat(), "mismatches": mismatches}


async def _verify_funding(client: httpx.AsyncClient, symbol: str, cache_dir: Path) -> dict[str, Any]:
    cached = _middle(_rows(cache_dir / "funding.json"))
    ts = _dt(cached["source_timestamp"])
    payload = await _get(client, "/v5/market/funding/history", {
        "category": "linear", "symbol": symbol, "limit": 20,
        "startTime": _ms(ts - dt.timedelta(hours=12)), "endTime": _ms(ts + dt.timedelta(hours=12)),
    })
    raw = _find_list_row(payload, _ms(ts), dict_timestamp_field="fundingRateTimestamp")
    raw_value = raw.get("fundingRate") if raw else None
    passed = raw_value is not None and _close(cached["funding_rate"], raw_value)
    return {"passed": passed, "timestamp": ts.isoformat(), "cached": cached.get("funding_rate"), "raw": raw_value}


async def verify_symbol(client: httpx.AsyncClient, symbol: str, cache_dir: Path) -> dict[str, Any]:
    checks = {
        "kline_1m": await _verify_kline(client, symbol, cache_dir, name="kline_1m", path="/v5/market/kline", interval="1", price_only=False),
        "kline_15m": await _verify_kline(client, symbol, cache_dir, name="kline_15m", path="/v5/market/kline", interval="15", price_only=False),
        "kline_1h": await _verify_kline(client, symbol, cache_dir, name="kline_1h", path="/v5/market/kline", interval="60", price_only=False),
        "kline_4h": await _verify_kline(client, symbol, cache_dir, name="kline_4h", path="/v5/market/kline", interval="240", price_only=False),
        "mark_5m": await _verify_kline(client, symbol, cache_dir, name="mark_5m", path="/v5/market/mark-price-kline", interval="5", price_only=True),
        "index_5m": await _verify_kline(client, symbol, cache_dir, name="index_5m", path="/v5/market/index-price-kline", interval="5", price_only=True),
        "open_interest_5m": await _verify_oi(client, symbol, cache_dir),
        "funding": await _verify_funding(client, symbol, cache_dir),
        "long_short_ratio_5m": await _verify_ratio(client, symbol, cache_dir),
    }
    return {"passed": all(row.get("passed") is True for row in checks.values()), "checks": checks}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.cache_root)
    summary = _payload(root / "backfill_summary.json")
    source = summary.get("results") or {}
    requested = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} if args.symbols else None
    results: dict[str, Any] = {}

    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "TV-BYBIT-CACHE-VERIFY/1.0"}) as client:
        for symbol, row in source.items():
            symbol_upper = str(symbol).upper()
            if requested is not None and symbol_upper not in requested:
                continue
            manifest = row.get("manifest") or {}
            cache_dir = manifest.get("cache_dir")
            if row.get("status") != "OK" or not cache_dir:
                results[symbol_upper] = {"passed": False, "error": "missing successful cache_dir"}
                continue
            try:
                results[symbol_upper] = await verify_symbol(client, symbol_upper, Path(cache_dir))
            except Exception as exc:
                results[symbol_upper] = {"passed": False, "error": repr(exc)}
            await asyncio.sleep(args.pause)

    if requested is not None:
        for symbol in sorted(requested - set(results)):
            results[symbol] = {"passed": False, "error": "requested symbol absent from backfill_summary"}

    passed = bool(results) and all(row.get("passed") is True for row in results.values())
    payload = {
        "verifier": VERIFY_VERSION,
        "api_base": OFFICIAL_API_BASE,
        "symbols_checked": len(results),
        "passed": passed,
        "results": results,
    }
    print(json.dumps(payload, indent=2))
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--symbols", help="optional comma-separated subset")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--pause", type=float, default=0.1)
    return parser.parse_args()


if __name__ == "__main__":
    result = asyncio.run(run(parse_args()))
    raise SystemExit(0 if result["passed"] else 1)
