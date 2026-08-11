#!/usr/bin/env python3
"""Validate reconstructed derivative cache against closed-kline availability.

Research-only safety check. Reads the existing extended cache and verifies that
for every derivative snapshot the stored mark/index price equals the latest 5m
price kline whose full interval had closed by the derivative timestamp.

No production DB writes, paper-state access, network calls or exchange actions.
Exit code is non-zero when any look-ahead mismatch or required-cache gap is
found.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT

UTC = dt.timezone.utc
FIVE_MINUTES = dt.timedelta(minutes=5)
VALIDATOR_VERSION = "EXTENDED_DERIVATIVES_CLOSED_5M_AVAILABILITY_V2_FAIL_CLOSED"
MAX_REPORTED_VIOLATIONS = 10


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out.astimezone(UTC)


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"cache rows must be a list: {path}")
    return [dict(row) for row in rows]


def _expected_closed_prices(rows: list[dict[str, Any]], *, source: str) -> tuple[list[dt.datetime], list[Decimal]]:
    if not rows:
        raise ValueError(f"{source} cache is empty")
    ordered = sorted(rows, key=lambda row: _dt(row["open_time"]))
    available_at = [_dt(row["open_time"]) + FIVE_MINUTES for row in ordered]
    closes = [_d(row.get("close")) for row in ordered]
    if any(value is None for value in closes):
        raise ValueError(f"{source} cache contains null close")
    return available_at, [value for value in closes if value is not None]


def _latest(available_at: list[dt.datetime], values: list[Decimal], at: dt.datetime) -> Decimal | None:
    idx = bisect.bisect_right(available_at, at) - 1
    return values[idx] if idx >= 0 else None


def validate_symbol(cache_dir: Path) -> dict[str, Any]:
    mark_rows = _rows(cache_dir / "mark_5m.json")
    index_rows = _rows(cache_dir / "index_5m.json")
    derivatives = _rows(cache_dir / "derivatives_reconstructed.json")
    if not derivatives:
        raise ValueError("derivatives_reconstructed cache is empty")

    mark_at, mark_close = _expected_closed_prices(mark_rows, source="mark_5m")
    index_at, index_close = _expected_closed_prices(index_rows, source="index_5m")

    first_violations: list[dict[str, Any]] = []
    violation_count = 0
    for row in derivatives:
        ts = _dt(row["source_timestamp"])
        expected_mark = _latest(mark_at, mark_close, ts)
        expected_index = _latest(index_at, index_close, ts)
        actual_mark = _d(row.get("mark_price"))
        actual_index = _d(row.get("index_price"))
        if actual_mark != expected_mark or actual_index != expected_index:
            violation_count += 1
            if len(first_violations) < MAX_REPORTED_VIOLATIONS:
                first_violations.append({
                    "source_timestamp": ts.isoformat(),
                    "actual_mark": str(actual_mark) if actual_mark is not None else None,
                    "expected_mark": str(expected_mark) if expected_mark is not None else None,
                    "actual_index": str(actual_index) if actual_index is not None else None,
                    "expected_index": str(expected_index) if expected_index is not None else None,
                })

    return {
        "cache_dir": str(cache_dir),
        "mark_rows": len(mark_rows),
        "index_rows": len(index_rows),
        "derivative_rows": len(derivatives),
        "violations": violation_count,
        "first_violations": first_violations,
        "passed": violation_count == 0,
    }


def main(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.cache_root)
    summary_path = root / "backfill_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_results = summary.get("results") or {}
    requested = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    results: dict[str, Any] = {}
    if requested is not None:
        for symbol in sorted(requested - {str(s).upper() for s in summary_results}):
            results[symbol] = {"passed": False, "error": "symbol absent from backfill_summary"}

    for symbol, row in summary_results.items():
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
        except Exception as exc:  # explicit audit output, then fail closed
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
    payload = main(parse_args())
    raise SystemExit(0 if payload["passed"] else 1)
