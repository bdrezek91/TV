"""Adjudicate public-trade vs kline-volume mismatches against raw Bybit archives.

This read-only research tool is intentionally separate from the strict data-fidelity
validator. The strict validator should continue to flag any disagreement between
cached public-trade aggregates and official 1m klines. This adjudicator determines
whether such a disagreement is caused by our cache/parser or already exists between
two official Bybit sources.

For every mismatching minute it:
1. reads cached 1m kline volume;
2. reads cached parsed public-trade buy/sell volume;
3. downloads the official daily public-trade CSV.gz from public.bybit.com;
4. independently parses raw ``timestamp``, ``side`` and ``size`` columns;
5. sums raw trades for the exact UTC minute;
6. classifies the mismatch as:
   - CACHE_OK: no mismatch after all;
   - OFFICIAL_SOURCE_DISAGREEMENT: raw archive == cache != kline;
   - CACHE_ERROR: raw archive != cache;
   - UNRESOLVED: raw fetch/parse failure or ambiguous result.

No production DB, paper state, order endpoint, or live service is modified.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, utc_dates

UTC = dt.timezone.utc
VERSION = "BYBIT_TRADE_VOLUME_MISMATCH_ADJUDICATOR_V1"
PUBLIC_BASE = "https://public.bybit.com/trading"
DEFAULT_REL_TOL = Decimal("0.000001")
EXACT_TOL = Decimal("0.000000000001")
MAX_EXAMPLES = 20


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value!r}")
    return out.astimezone(UTC)


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict[str, Any]]:
    payload = _json(path)
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"rows must be a list: {path}")
    return [dict(row) for row in rows]


def _parse_raw_timestamp(raw: str) -> dt.datetime:
    value = Decimal(str(raw))
    # Public archives normally use Unix seconds with sub-second precision, while
    # the fallback also safely accepts millisecond epochs.
    seconds = value / Decimal("1000") if value >= Decimal("100000000000") else value
    return dt.datetime.fromtimestamp(float(seconds), tz=UTC)


def _minute(ts: dt.datetime) -> dt.datetime:
    return ts.replace(second=0, microsecond=0)


def _relative_error(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / max(abs(b), Decimal("1e-18"))


def _close(a: Decimal, b: Decimal, *, rel_tol: Decimal) -> bool:
    return abs(a - b) <= max(EXACT_TOL, rel_tol * max(abs(a), abs(b), Decimal("1e-18")))


def _cached_trade_minutes(cache_dir: Path, start: dt.datetime, end: dt.datetime) -> dict[dt.datetime, dict[str, Decimal]]:
    result: dict[dt.datetime, dict[str, Decimal]] = {}
    daily_root = cache_dir / "trades_1m_daily"
    for day in utc_dates(start, end):
        path = daily_root / f"{day.isoformat()}.json"
        if not path.is_file():
            raise ValueError(f"trade archive day missing: {path}")
        payload = _json(path)
        if isinstance(payload, dict) and payload.get("missing") is True:
            raise ValueError(f"trade archive explicitly missing: {path}")
        rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError(f"invalid trade archive rows: {path}")
        for row in rows:
            ts = _dt(row["bucket_start"])
            buy = _d(row.get("buy_taker_volume", "0"))
            sell = _d(row.get("sell_taker_volume", "0"))
            result[ts] = {"buy": buy, "sell": sell, "total": buy + sell}
    return result


def find_mismatches(cache_dir: Path, *, rel_tol: Decimal) -> list[dict[str, Any]]:
    manifest = _json(cache_dir / "manifest.json")
    start = _dt(manifest["requested_start"])
    end = _dt(manifest["requested_end"])
    candles = {_dt(row["open_time"]): _d(row["volume"]) for row in _rows(cache_dir / "kline_1m.json")}
    trades = _cached_trade_minutes(cache_dir, start, end)

    mismatches: list[dict[str, Any]] = []
    for ts, kline_volume in sorted(candles.items()):
        if ts < start or ts > end:
            continue
        cached = trades.get(ts, {"buy": Decimal("0"), "sell": Decimal("0"), "total": Decimal("0")})
        trade_volume = cached["total"]
        if not _close(trade_volume, kline_volume, rel_tol=rel_tol):
            mismatches.append({
                "minute": ts,
                "kline_volume": kline_volume,
                "cached_buy": cached["buy"],
                "cached_sell": cached["sell"],
                "cached_trade_volume": trade_volume,
                "relative_error": _relative_error(trade_volume, kline_volume),
            })
    return mismatches


def _archive_url(symbol: str, day: dt.date) -> str:
    return f"{PUBLIC_BASE}/{symbol}/{symbol}{day.isoformat()}.csv.gz"


def fetch_raw_day(client: httpx.Client, symbol: str, day: dt.date) -> bytes:
    url = _archive_url(symbol, day)
    response = client.get(url)
    response.raise_for_status()
    return response.content


def aggregate_raw_minutes(raw_gz: bytes, wanted: set[dt.datetime]) -> dict[dt.datetime, dict[str, Any]]:
    result: dict[dt.datetime, dict[str, Any]] = {
        ts: {
            "buy": Decimal("0"),
            "sell": Decimal("0"),
            "total": Decimal("0"),
            "trade_count": 0,
            "first_trade": None,
            "last_trade": None,
        }
        for ts in wanted
    }
    with gzip.GzipFile(fileobj=io.BytesIO(raw_gz), mode="rb") as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        reader = csv.DictReader(text)
        required = {"timestamp", "side", "size"}
        headers = {str(name) for name in (reader.fieldnames or [])}
        if not required.issubset(headers):
            raise ValueError(f"unexpected public-trade CSV headers: {reader.fieldnames!r}")
        for row in reader:
            ts = _parse_raw_timestamp(row["timestamp"])
            bucket = _minute(ts)
            if bucket not in result:
                continue
            size = _d(row["size"])
            side = str(row["side"]).strip().lower()
            if side == "buy":
                result[bucket]["buy"] += size
            elif side == "sell":
                result[bucket]["sell"] += size
            else:
                continue
            result[bucket]["total"] += size
            result[bucket]["trade_count"] += 1
            first_trade = result[bucket]["first_trade"]
            last_trade = result[bucket]["last_trade"]
            result[bucket]["first_trade"] = ts if first_trade is None or ts < first_trade else first_trade
            result[bucket]["last_trade"] = ts if last_trade is None or ts > last_trade else last_trade
    return result


def adjudicate_symbol(
    client: httpx.Client,
    symbol: str,
    cache_dir: Path,
    *,
    rel_tol: Decimal,
) -> dict[str, Any]:
    mismatches = find_mismatches(cache_dir, rel_tol=rel_tol)
    if not mismatches:
        return {
            "passed": True,
            "mismatch_count": 0,
            "cache_errors": 0,
            "official_source_disagreements": 0,
            "unresolved": 0,
            "adjudications": [],
        }

    by_day: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for row in mismatches:
        by_day[row["minute"].date()].append(row)

    adjudications: list[dict[str, Any]] = []
    for day, rows in sorted(by_day.items()):
        wanted = {row["minute"] for row in rows}
        try:
            raw = aggregate_raw_minutes(fetch_raw_day(client, symbol, day), wanted)
        except Exception as exc:
            for row in rows:
                adjudications.append({
                    "minute": row["minute"].isoformat(),
                    "classification": "UNRESOLVED",
                    "error": repr(exc),
                })
            continue

        for row in rows:
            ts = row["minute"]
            raw_row = raw[ts]
            cached = row["cached_trade_volume"]
            kline = row["kline_volume"]
            raw_total = raw_row["total"]
            cache_matches_raw = _close(cached, raw_total, rel_tol=EXACT_TOL)
            raw_matches_kline = _close(raw_total, kline, rel_tol=rel_tol)

            if cache_matches_raw and not raw_matches_kline:
                classification = "OFFICIAL_SOURCE_DISAGREEMENT"
            elif not cache_matches_raw:
                classification = "CACHE_ERROR"
            else:
                classification = "CACHE_OK"

            adjudications.append({
                "minute": ts.isoformat(),
                "classification": classification,
                "kline_volume": str(kline),
                "cached_trade_volume": str(cached),
                "raw_trade_volume": str(raw_total),
                "cached_buy": str(row["cached_buy"]),
                "cached_sell": str(row["cached_sell"]),
                "raw_buy": str(raw_row["buy"]),
                "raw_sell": str(raw_row["sell"]),
                "trade_count": raw_row["trade_count"],
                "relative_error_vs_kline": str(_relative_error(raw_total, kline)),
                "first_trade": raw_row["first_trade"].isoformat() if raw_row["first_trade"] else None,
                "last_trade": raw_row["last_trade"].isoformat() if raw_row["last_trade"] else None,
                "source_url": _archive_url(symbol, day),
            })

    cache_errors = sum(row.get("classification") == "CACHE_ERROR" for row in adjudications)
    source_disagreements = sum(row.get("classification") == "OFFICIAL_SOURCE_DISAGREEMENT" for row in adjudications)
    unresolved = sum(row.get("classification") == "UNRESOLVED" for row in adjudications)
    return {
        "passed": cache_errors == 0 and unresolved == 0,
        "mismatch_count": len(mismatches),
        "cache_errors": cache_errors,
        "official_source_disagreements": source_disagreements,
        "unresolved": unresolved,
        "adjudications": adjudications[:MAX_EXAMPLES],
        "note": "passed=True means every strict kline-vs-trade mismatch was proven against the raw official Bybit archive to be cache-faithful or resolved; official-source disagreements remain explicitly reported.",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--symbols", help="optional comma-separated subset; default all symbols in backfill_summary")
    parser.add_argument("--rel-tolerance", default=str(DEFAULT_REL_TOL))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", help="optional JSON report path")
    return parser.parse_args()


def main(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.cache_root)
    summary = _json(root / "backfill_summary.json")
    source = summary.get("results") or {}
    selected = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else [str(s).upper() for s in summary.get("symbols", [])]
    )
    rel_tol = Decimal(str(args.rel_tolerance))
    results: dict[str, Any] = {}

    with httpx.Client(timeout=args.timeout, headers={"User-Agent": "TV-BYBIT-MISMATCH-ADJUDICATOR/1.0"}) as client:
        for symbol in selected:
            row = source.get(symbol)
            cache_dir_raw = ((row or {}).get("manifest") or {}).get("cache_dir")
            if not row or row.get("status") != "OK" or not cache_dir_raw:
                results[symbol] = {"passed": False, "error": "symbol absent or not OK in backfill_summary"}
                continue
            try:
                results[symbol] = adjudicate_symbol(client, symbol, Path(cache_dir_raw), rel_tol=rel_tol)
            except Exception as exc:
                results[symbol] = {"passed": False, "error": repr(exc)}

    passed = bool(results) and all(row.get("passed") is True for row in results.values())
    payload = {
        "adjudicator": VERSION,
        "cache_root": str(root),
        "symbols_checked": len(results),
        "passed": passed,
        "results": results,
    }
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    return payload


if __name__ == "__main__":
    report = main(parse_args())
    raise SystemExit(0 if report["passed"] else 1)
