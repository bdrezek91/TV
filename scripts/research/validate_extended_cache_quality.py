#!/usr/bin/env python3
"""Validate structural and cross-source quality of cached official Bybit history.

Research-only, read-only validator. It checks that cached market data is not only
present, but internally consistent:
- kline timestamps are unique, aligned and continuous at the advertised cadence;
- 1m OHLCV aggregates reproduce cached 15m OHLCV;
- mark/index price klines are continuous on a 5m grid;
- OI and long/short ratio observations are unique and monotonic on the 5m grid;
- reconstructed derivative timestamps are a subset of cached OI timestamps;
- public-trade 1m taker volume reconciles to trade-price 1m kline volume.

The validator makes no network calls and writes no production state. It exits
non-zero when any invariant fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, utc_dates

UTC = dt.timezone.utc
VALIDATOR_VERSION = "EXTENDED_CACHE_QUALITY_V1"
REL_TOL = Decimal("0.000001")  # 1 ppm, only for source rounding noise
ABS_TOL = Decimal("0.00000001")
MAX_EXAMPLES = 10


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out.astimezone(UTC)


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _payload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"required cache file missing: {path.name}")
    payload = _payload(path)
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"required cache rows empty/invalid: {path.name}")
    return [dict(row) for row in rows]


def _close(a: Decimal, b: Decimal) -> bool:
    diff = abs(a - b)
    scale = max(abs(a), abs(b), Decimal("1"))
    return diff <= max(ABS_TOL, REL_TOL * scale)


def _grid_diagnostics(rows: Iterable[dict[str, Any]], *, field: str, seconds: int, name: str) -> dict[str, Any]:
    timestamps = sorted(_dt(row[field]) for row in rows)
    duplicates = len(timestamps) - len(set(timestamps))
    off_grid = [ts.isoformat() for ts in timestamps if int(ts.timestamp()) % seconds != 0]
    gaps: list[dict[str, Any]] = []
    for prev, cur in zip(timestamps, timestamps[1:]):
        delta = int((cur - prev).total_seconds())
        if delta != seconds:
            gaps.append({"from": prev.isoformat(), "to": cur.isoformat(), "seconds": delta})
    return {
        "name": name,
        "rows": len(timestamps),
        "duplicates": duplicates,
        "off_grid": off_grid[:MAX_EXAMPLES],
        "off_grid_count": len(off_grid),
        "gaps": gaps[:MAX_EXAMPLES],
        "gap_count": len(gaps),
        "passed": duplicates == 0 and not off_grid and not gaps,
    }


def _aggregate_1m_to_15m(c1m: list[dict[str, Any]], c15: list[dict[str, Any]]) -> dict[str, Any]:
    one = {_dt(row["open_time"]): row for row in c1m}
    mismatches: list[dict[str, Any]] = []
    compared = 0
    incomplete = 0
    for bar in c15:
        start = _dt(bar["open_time"])
        members = [one.get(start + dt.timedelta(minutes=i)) for i in range(15)]
        if any(row is None for row in members):
            incomplete += 1
            continue
        rows = [row for row in members if row is not None]
        compared += 1
        expected = {
            "open": _d(rows[0]["open"]),
            "high": max(_d(row["high"]) for row in rows),
            "low": min(_d(row["low"]) for row in rows),
            "close": _d(rows[-1]["close"]),
            "volume": sum((_d(row["volume"]) for row in rows), Decimal("0")),
        }
        actual = {key: _d(bar[key]) for key in expected}
        bad = [key for key in expected if not _close(actual[key], expected[key])]
        if bad and len(mismatches) < MAX_EXAMPLES:
            mismatches.append({
                "open_time": start.isoformat(),
                "fields": bad,
                "expected": {k: str(expected[k]) for k in bad},
                "actual": {k: str(actual[k]) for k in bad},
            })
    return {
        "compared_complete_15m_bars": compared,
        "incomplete_15m_bars_skipped": incomplete,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "passed": compared > 0 and not mismatches,
    }


def _trade_daily_rows(cache_dir: Path, requested_start: dt.datetime, requested_end: dt.datetime) -> list[dict[str, Any]]:
    root = cache_dir / "trades_1m_daily"
    rows: list[dict[str, Any]] = []
    for day in utc_dates(requested_start, requested_end):
        path = root / f"{day.isoformat()}.json"
        if not path.is_file():
            raise ValueError(f"trade archive day missing: {day.isoformat()}")
        payload = _payload(path)
        if isinstance(payload, dict) and payload.get("missing") is True:
            raise ValueError(f"trade archive explicitly missing: {day.isoformat()}")
        day_rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        if not isinstance(day_rows, list):
            raise ValueError(f"invalid trade archive rows: {day.isoformat()}")
        rows.extend(dict(row) for row in day_rows)
    return rows


def _trade_volume_reconciliation(c1m: list[dict[str, Any]], trades: list[dict[str, Any]]) -> dict[str, Any]:
    candle = {_dt(row["open_time"]): _d(row["volume"]) for row in c1m}
    taker: dict[dt.datetime, Decimal] = defaultdict(lambda: Decimal("0"))
    duplicates = 0
    seen: set[dt.datetime] = set()
    for row in trades:
        ts = _dt(row["bucket_start"])
        if ts in seen:
            duplicates += 1
        seen.add(ts)
        taker[ts] += _d(row.get("buy_taker_volume", "0")) + _d(row.get("sell_taker_volume", "0"))

    mismatches: list[dict[str, Any]] = []
    compared = 0
    # Compare only timestamps represented by the public-trade archive; missing
    # positive-volume candle buckets are counted separately below.
    for ts, trade_volume in taker.items():
        if ts not in candle:
            if len(mismatches) < MAX_EXAMPLES:
                mismatches.append({"bucket_start": ts.isoformat(), "error": "trade bucket absent from kline_1m"})
            continue
        compared += 1
        if not _close(trade_volume, candle[ts]) and len(mismatches) < MAX_EXAMPLES:
            mismatches.append({
                "bucket_start": ts.isoformat(),
                "trade_volume": str(trade_volume),
                "kline_volume": str(candle[ts]),
            })

    if taker:
        first_trade, last_trade = min(taker), max(taker)
        missing_positive = [
            ts for ts, volume in candle.items()
            if first_trade <= ts <= last_trade and volume > 0 and ts not in taker
        ]
    else:
        missing_positive = list(candle)

    return {
        "trade_buckets": len(taker),
        "compared_buckets": compared,
        "duplicate_trade_buckets": duplicates,
        "missing_positive_volume_buckets": len(missing_positive),
        "missing_positive_examples": [ts.isoformat() for ts in sorted(missing_positive)[:MAX_EXAMPLES]],
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "passed": bool(taker) and duplicates == 0 and not missing_positive and not mismatches,
    }


def _derivative_anchor_check(oi: list[dict[str, Any]], deriv: list[dict[str, Any]]) -> dict[str, Any]:
    oi_ts = {_dt(row["source_timestamp"]) for row in oi}
    deriv_ts = [_dt(row["source_timestamp"]) for row in deriv]
    extra = sorted(set(deriv_ts) - oi_ts)
    duplicates = len(deriv_ts) - len(set(deriv_ts))
    return {
        "oi_rows": len(oi_ts),
        "derivative_rows": len(deriv_ts),
        "duplicate_derivative_timestamps": duplicates,
        "non_oi_anchor_count": len(extra),
        "non_oi_anchor_examples": [ts.isoformat() for ts in extra[:MAX_EXAMPLES]],
        "passed": duplicates == 0 and not extra and bool(deriv_ts),
    }


def validate_symbol(cache_dir: Path) -> dict[str, Any]:
    manifest = _payload(cache_dir / "manifest.json")
    start = _dt(manifest["requested_start"])
    end = _dt(manifest["requested_end"])

    c1m = _rows(cache_dir / "kline_1m.json")
    c15 = _rows(cache_dir / "kline_15m.json")
    c1h = _rows(cache_dir / "kline_1h.json")
    c4h = _rows(cache_dir / "kline_4h.json")
    mark = _rows(cache_dir / "mark_5m.json")
    index = _rows(cache_dir / "index_5m.json")
    oi = _rows(cache_dir / "open_interest_5m.json")
    ratio = _rows(cache_dir / "long_short_ratio_5m.json")
    funding = _rows(cache_dir / "funding.json")
    deriv = _rows(cache_dir / "derivatives_reconstructed.json")
    trades = _trade_daily_rows(cache_dir, start, end)

    checks: dict[str, Any] = {
        "kline_1m_grid": _grid_diagnostics(c1m, field="open_time", seconds=60, name="kline_1m"),
        "kline_15m_grid": _grid_diagnostics(c15, field="open_time", seconds=900, name="kline_15m"),
        "kline_1h_grid": _grid_diagnostics(c1h, field="open_time", seconds=3600, name="kline_1h"),
        "kline_4h_grid": _grid_diagnostics(c4h, field="open_time", seconds=14400, name="kline_4h"),
        "mark_5m_grid": _grid_diagnostics(mark, field="open_time", seconds=300, name="mark_5m"),
        "index_5m_grid": _grid_diagnostics(index, field="open_time", seconds=300, name="index_5m"),
        "oi_5m_grid": _grid_diagnostics(oi, field="source_timestamp", seconds=300, name="open_interest_5m"),
        "ratio_5m_grid": _grid_diagnostics(ratio, field="source_timestamp", seconds=300, name="long_short_ratio_5m"),
        "funding_unique_monotonic": _grid_diagnostics(funding, field="source_timestamp", seconds=28800, name="funding_8h_expected"),
        "one_to_fifteen_ohlcv": _aggregate_1m_to_15m(c1m, c15),
        "trade_volume_vs_kline_1m": _trade_volume_reconciliation(c1m, trades),
        "derivative_anchor": _derivative_anchor_check(oi, deriv),
    }
    passed = all(check.get("passed") is True for check in checks.values())
    return {"cache_dir": str(cache_dir), "passed": passed, "checks": checks}


def main(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.cache_root)
    summary = _payload(root / "backfill_summary.json")
    results_src = summary.get("results") or {}
    requested = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} if args.symbols else None
    results: dict[str, Any] = {}

    if requested is not None:
        absent = requested - {str(symbol).upper() for symbol in results_src}
        for symbol in sorted(absent):
            results[symbol] = {"passed": False, "error": "symbol absent from backfill_summary"}

    for symbol, row in results_src.items():
        symbol_upper = str(symbol).upper()
        if requested is not None and symbol_upper not in requested:
            continue
        manifest = row.get("manifest") or {}
        cache_dir = manifest.get("cache_dir")
        if row.get("status") != "OK" or not cache_dir:
            results[symbol_upper] = {"passed": False, "error": "missing successful cache_dir"}
            continue
        try:
            results[symbol_upper] = validate_symbol(Path(cache_dir))
        except Exception as exc:
            results[symbol_upper] = {"passed": False, "error": repr(exc)}

    passed = bool(results) and all(row.get("passed") is True for row in results.values())
    payload = {
        "validator": VALIDATOR_VERSION,
        "cache_root": str(root),
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
    return parser.parse_args()


if __name__ == "__main__":
    result = main(parse_args())
    raise SystemExit(0 if result["passed"] else 1)
