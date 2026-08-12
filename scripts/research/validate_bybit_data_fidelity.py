"""Fail-closed audit of cached BACKTEST_COMPARISON_V1 Bybit source data.

This validator is deliberately independent from strategy logic. It verifies:
- exact candle-grid coverage and OHLC/volume sanity for cached trade/mark/index klines;
- 5m OI and long/short-ratio coverage, uniqueness and value invariants;
- funding timestamp/value sanity without assuming a universal funding interval;
- public-trade 1m aggregates against independent Bybit 1m kline volume;
- optional raw REST spot checks, comparing cache rows to unparsed Bybit V5 payloads.

The optional online checks intentionally do NOT call the research downloader/parser.
They use raw httpx responses and compare fields directly, so a parser bug cannot
validate itself.

Research-only: no production DB, paper state or order endpoint is touched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import httpx

from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT, OFFICIAL_API_BASE, utc_dates

UTC = dt.timezone.utc
VERSION = "BYBIT_DATA_FIDELITY_V1"
MAX_EXAMPLES = 10

CANDLE_DATASETS: dict[str, tuple[int, bool]] = {
    "kline_1m": (60, True),
    "kline_15m": (15 * 60, True),
    "kline_1h": (60 * 60, True),
    "kline_4h": (4 * 60 * 60, True),
    "mark_5m": (5 * 60, False),
    "index_5m": (5 * 60, False),
}

ONLINE_SPECS = {
    "kline_1m": ("/v5/market/kline", "candle", {"interval": "1"}, 60),
    "kline_15m": ("/v5/market/kline", "candle", {"interval": "15"}, 15 * 60),
    "kline_1h": ("/v5/market/kline", "candle", {"interval": "60"}, 60 * 60),
    "kline_4h": ("/v5/market/kline", "candle", {"interval": "240"}, 4 * 60 * 60),
    "mark_5m": ("/v5/market/mark-price-kline", "price_candle", {"interval": "5"}, 5 * 60),
    "index_5m": ("/v5/market/index-price-kline", "price_candle", {"interval": "5"}, 5 * 60),
    "open_interest_5m": ("/v5/market/open-interest", "oi", {"intervalTime": "5min"}, 5 * 60),
    "funding": ("/v5/market/funding/history", "funding", {}, None),
    "long_short_ratio_5m": ("/v5/market/account-ratio", "ratio", {"period": "5min"}, 5 * 60),
}


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


def _ceil_grid(ts: dt.datetime, step_seconds: int) -> dt.datetime:
    epoch = ts.timestamp()
    aligned = math.ceil(epoch / step_seconds) * step_seconds
    return dt.datetime.fromtimestamp(aligned, tz=UTC)


def _floor_grid(ts: dt.datetime, step_seconds: int) -> dt.datetime:
    epoch = ts.timestamp()
    aligned = math.floor(epoch / step_seconds) * step_seconds
    return dt.datetime.fromtimestamp(aligned, tz=UTC)


def _expected_grid(start: dt.datetime, end: dt.datetime, step_seconds: int) -> list[dt.datetime]:
    first = _ceil_grid(start, step_seconds)
    last = _floor_grid(end, step_seconds)
    if first > last:
        return []
    n = int((last - first).total_seconds() // step_seconds) + 1
    return [first + dt.timedelta(seconds=step_seconds * i) for i in range(n)]


def _sample_indices(length: int, count: int) -> list[int]:
    if length <= 0 or count <= 0:
        return []
    count = min(length, count)
    if count == 1:
        return [length // 2]
    return sorted({round(i * (length - 1) / (count - 1)) for i in range(count)})


def _record_example(examples: list[Any], value: Any) -> None:
    if len(examples) < MAX_EXAMPLES:
        examples.append(value)


def validate_candle_dataset(
    rows: list[dict[str, Any]],
    *,
    start: dt.datetime,
    end: dt.datetime,
    step_seconds: int,
    require_volume: bool,
) -> dict[str, Any]:
    errors: list[str] = []
    examples: list[Any] = []
    if not rows:
        return {"passed": False, "rows": 0, "errors": ["empty dataset"], "examples": []}

    actual_times: list[dt.datetime] = []
    seen: set[dt.datetime] = set()
    duplicates = 0
    sanity_failures = 0
    for row in rows:
        ts = _dt(row["open_time"])
        actual_times.append(ts)
        if ts in seen:
            duplicates += 1
            _record_example(examples, {"duplicate_open_time": ts.isoformat()})
        seen.add(ts)
        try:
            o, h, l, c = (_d(row[k]) for k in ("open", "high", "low", "close"))
            if l > min(o, c) or h < max(o, c) or l > h:
                sanity_failures += 1
                _record_example(examples, {"bad_ohlc": ts.isoformat(), "open": str(o), "high": str(h), "low": str(l), "close": str(c)})
            if require_volume:
                volume = _d(row.get("volume", "0"))
                turnover = _d(row.get("turnover", "0")) if row.get("turnover") is not None else Decimal("0")
                if volume < 0 or turnover < 0:
                    sanity_failures += 1
                    _record_example(examples, {"negative_volume_or_turnover": ts.isoformat()})
        except Exception as exc:
            sanity_failures += 1
            _record_example(examples, {"parse_error": ts.isoformat(), "error": repr(exc)})

    expected = set(_expected_grid(start, end, step_seconds))
    actual = set(actual_times)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    misaligned = sorted(ts for ts in actual if int(ts.timestamp()) % step_seconds != 0)

    if duplicates:
        errors.append(f"duplicate timestamps: {duplicates}")
    if missing:
        errors.append(f"missing expected timestamps: {len(missing)}")
        for ts in missing[:MAX_EXAMPLES]:
            _record_example(examples, {"missing": ts.isoformat()})
    if extra:
        errors.append(f"timestamps outside requested grid: {len(extra)}")
    if misaligned:
        errors.append(f"misaligned timestamps: {len(misaligned)}")
    if sanity_failures:
        errors.append(f"OHLC/value sanity failures: {sanity_failures}")

    return {
        "passed": not errors,
        "rows": len(rows),
        "expected_rows": len(expected),
        "duplicates": duplicates,
        "missing": len(missing),
        "extra": len(extra),
        "misaligned": len(misaligned),
        "sanity_failures": sanity_failures,
        "errors": errors,
        "examples": examples,
    }


def validate_5m_point_dataset(
    rows: list[dict[str, Any]],
    *,
    start: dt.datetime,
    end: dt.datetime,
    kind: str,
    required_coverage: Decimal,
) -> dict[str, Any]:
    errors: list[str] = []
    examples: list[Any] = []
    if not rows:
        return {"passed": False, "rows": 0, "errors": ["empty dataset"], "examples": []}

    times: list[dt.datetime] = []
    seen: set[dt.datetime] = set()
    duplicates = 0
    value_failures = 0
    for row in rows:
        ts = _dt(row["source_timestamp"])
        times.append(ts)
        if ts in seen:
            duplicates += 1
        seen.add(ts)
        try:
            if kind == "oi":
                if _d(row["open_interest"]) < 0:
                    raise ValueError("negative open interest")
            elif kind == "ratio":
                buy, sell = _d(row["buy_ratio"]), _d(row["sell_ratio"])
                if not (Decimal("0") <= buy <= Decimal("1") and Decimal("0") <= sell <= Decimal("1")):
                    raise ValueError("ratio outside 0..1")
                if abs((buy + sell) - Decimal("1")) > Decimal("0.02"):
                    raise ValueError("buy+sell differs from 1 by >0.02")
                expected_ratio = buy / sell if sell else None
                actual_ratio = _d(row["long_short_ratio"]) if row.get("long_short_ratio") is not None else None
                if expected_ratio != actual_ratio:
                    raise ValueError("computed long_short_ratio mismatch")
        except Exception as exc:
            value_failures += 1
            _record_example(examples, {"timestamp": ts.isoformat(), "error": repr(exc)})

    expected = set(_expected_grid(start, end, 300))
    actual = set(times)
    missing = expected - actual
    extra = actual - expected
    coverage = (Decimal(len(expected & actual)) / Decimal(len(expected))) if expected else Decimal("0")
    misaligned = [ts for ts in actual if int(ts.timestamp()) % 300 != 0]

    if duplicates:
        errors.append(f"duplicate timestamps: {duplicates}")
    if coverage < required_coverage:
        errors.append(f"coverage {coverage} below required {required_coverage}")
    if extra:
        errors.append(f"timestamps outside requested grid: {len(extra)}")
    if misaligned:
        errors.append(f"misaligned timestamps: {len(misaligned)}")
    if value_failures:
        errors.append(f"value invariant failures: {value_failures}")

    for ts in sorted(missing)[:MAX_EXAMPLES]:
        _record_example(examples, {"missing": ts.isoformat()})

    return {
        "passed": not errors,
        "rows": len(rows),
        "expected_rows": len(expected),
        "coverage": str(coverage),
        "missing": len(missing),
        "extra": len(extra),
        "duplicates": duplicates,
        "misaligned": len(misaligned),
        "value_failures": value_failures,
        "errors": errors,
        "examples": examples,
    }


def validate_funding(rows: list[dict[str, Any]], *, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    errors: list[str] = []
    examples: list[Any] = []
    if not rows:
        return {"passed": False, "rows": 0, "errors": ["empty dataset"], "examples": []}
    times: list[dt.datetime] = []
    bad = 0
    for row in rows:
        ts = _dt(row["source_timestamp"])
        times.append(ts)
        try:
            rate = _d(row["funding_rate"])
            if not rate.is_finite():
                raise ValueError("non-finite funding rate")
        except Exception as exc:
            bad += 1
            _record_example(examples, {"timestamp": ts.isoformat(), "error": repr(exc)})
    duplicates = len(times) - len(set(times))
    outside = [ts for ts in times if ts < start or ts > end]
    if duplicates:
        errors.append(f"duplicate timestamps: {duplicates}")
    if outside:
        errors.append(f"timestamps outside requested range: {len(outside)}")
    if bad:
        errors.append(f"invalid funding values: {bad}")
    return {
        "passed": not errors,
        "rows": len(rows),
        "duplicates": duplicates,
        "outside_range": len(outside),
        "errors": errors,
        "examples": examples,
        "note": "Funding interval is symbol-specific; cadence is not hard-coded here.",
    }


def validate_trade_volume_against_kline(
    cache_dir: Path,
    *,
    start: dt.datetime,
    end: dt.datetime,
    rel_tolerance: Decimal,
) -> dict[str, Any]:
    errors: list[str] = []
    examples: list[Any] = []
    candle_rows = _rows(cache_dir / "kline_1m.json")
    candle_volume = {_dt(row["open_time"]): _d(row["volume"]) for row in candle_rows}

    daily_root = cache_dir / "trades_1m_daily"
    if not daily_root.is_dir():
        return {"passed": False, "errors": ["trades_1m_daily missing"], "examples": []}

    trade_by_minute: dict[dt.datetime, Decimal] = {}
    delta_failures = 0
    missing_markers: list[str] = []
    absent_days: list[str] = []
    expected_days = utc_dates(start, end)
    for day in expected_days:
        path = daily_root / f"{day.isoformat()}.json"
        if not path.is_file():
            absent_days.append(day.isoformat())
            continue
        payload = _json(path)
        if isinstance(payload, dict) and payload.get("missing") is True:
            missing_markers.append(day.isoformat())
            continue
        rows = payload.get("rows", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            errors.append(f"invalid trade archive payload: {path.name}")
            continue
        for row in rows:
            ts = _dt(row["bucket_start"])
            buy = _d(row.get("buy_taker_volume", "0"))
            sell = _d(row.get("sell_taker_volume", "0"))
            delta = _d(row.get("delta", buy - sell))
            if buy < 0 or sell < 0 or delta != buy - sell:
                delta_failures += 1
                _record_example(examples, {"trade_invariant": ts.isoformat(), "buy": str(buy), "sell": str(sell), "delta": str(delta)})
            if ts in trade_by_minute:
                errors.append(f"duplicate trade minute across daily files: {ts.isoformat()}")
            trade_by_minute[ts] = buy + sell

    if absent_days:
        errors.append(f"absent daily trade archives: {len(absent_days)}")
    if missing_markers:
        errors.append(f"daily trade archives marked missing: {len(missing_markers)}")
    if delta_failures:
        errors.append(f"trade aggregate invariant failures: {delta_failures}")

    compared = 0
    mismatches = 0
    max_rel_error = Decimal("0")
    total_kline_volume = Decimal("0")
    total_trade_volume = Decimal("0")
    for ts, kvol in sorted(candle_volume.items()):
        if ts < start or ts > end:
            continue
        tvol = trade_by_minute.get(ts, Decimal("0"))
        compared += 1
        total_kline_volume += kvol
        total_trade_volume += tvol
        diff = abs(tvol - kvol)
        denom = max(abs(kvol), Decimal("1e-18"))
        rel = diff / denom
        max_rel_error = max(max_rel_error, rel)
        if diff != 0 and rel > rel_tolerance:
            mismatches += 1
            _record_example(examples, {"minute": ts.isoformat(), "kline_volume": str(kvol), "trade_volume": str(tvol), "relative_error": str(rel)})

    if compared == 0:
        errors.append("no 1m candle minutes available for trade-volume comparison")
    if mismatches:
        errors.append(f"public-trade vs kline volume mismatches above tolerance: {mismatches}/{compared}")

    total_rel_error = abs(total_trade_volume - total_kline_volume) / max(abs(total_kline_volume), Decimal("1e-18"))
    return {
        "passed": not errors,
        "compared_minutes": compared,
        "mismatches": mismatches,
        "rel_tolerance": str(rel_tolerance),
        "max_relative_error": str(max_rel_error),
        "total_kline_volume": str(total_kline_volume),
        "total_trade_volume": str(total_trade_volume),
        "total_relative_error": str(total_rel_error),
        "absent_days": absent_days,
        "missing_markers": missing_markers,
        "delta_failures": delta_failures,
        "errors": errors,
        "examples": examples,
    }


def validate_symbol_cache(cache_dir: Path, *, required_5m_coverage: Decimal, volume_rel_tolerance: Decimal) -> dict[str, Any]:
    manifest = _json(cache_dir / "manifest.json")
    start = _dt(manifest["requested_start"])
    end = _dt(manifest["requested_end"])
    checks: dict[str, Any] = {}

    for name, (step, require_volume) in CANDLE_DATASETS.items():
        checks[name] = validate_candle_dataset(
            _rows(cache_dir / f"{name}.json"),
            start=start,
            end=end,
            step_seconds=step,
            require_volume=require_volume,
        )

    checks["open_interest_5m"] = validate_5m_point_dataset(
        _rows(cache_dir / "open_interest_5m.json"),
        start=start,
        end=end,
        kind="oi",
        required_coverage=required_5m_coverage,
    )
    checks["long_short_ratio_5m"] = validate_5m_point_dataset(
        _rows(cache_dir / "long_short_ratio_5m.json"),
        start=start,
        end=end,
        kind="ratio",
        required_coverage=required_5m_coverage,
    )
    checks["funding"] = validate_funding(_rows(cache_dir / "funding.json"), start=start, end=end)
    checks["public_trades_vs_kline_1m"] = validate_trade_volume_against_kline(
        cache_dir,
        start=start,
        end=end,
        rel_tolerance=volume_rel_tolerance,
    )
    passed = all(check.get("passed") is True for check in checks.values())
    return {
        "passed": passed,
        "cache_dir": str(cache_dir),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "checks": checks,
    }


def _raw_get(client: httpx.Client, path: str, params: dict[str, Any], *, retries: int, pause: float) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("retCode", -1)) != 0:
                raise RuntimeError(f"Bybit retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}")
            if pause > 0:
                time.sleep(pause)
            return payload
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(max(pause, 0.25) * (2 ** attempt))
    raise RuntimeError(f"raw Bybit request failed: {path}: {last!r}")


def _find_raw_list_row(payload: dict[str, Any], *, kind: str, timestamp_ms: int) -> Any | None:
    rows = (payload.get("result") or {}).get("list") or []
    if kind in {"candle", "price_candle"}:
        for row in rows:
            if row and int(row[0]) == timestamp_ms:
                return row
        return None
    ts_key = {"oi": "timestamp", "funding": "fundingRateTimestamp", "ratio": "timestamp"}[kind]
    for row in rows:
        if int(row.get(ts_key, -1)) == timestamp_ms:
            return row
    return None


def _compare_raw(cache_row: dict[str, Any], raw_row: Any, *, kind: str) -> list[str]:
    mismatches: list[str] = []
    if kind == "candle":
        mapping = [("open", 1), ("high", 2), ("low", 3), ("close", 4), ("volume", 5), ("turnover", 6)]
        for field, idx in mapping:
            if _d(cache_row[field]) != _d(raw_row[idx]):
                mismatches.append(field)
    elif kind == "price_candle":
        for field, idx in [("open", 1), ("high", 2), ("low", 3), ("close", 4)]:
            if _d(cache_row[field]) != _d(raw_row[idx]):
                mismatches.append(field)
    elif kind == "oi":
        if _d(cache_row["open_interest"]) != _d(raw_row["openInterest"]):
            mismatches.append("open_interest")
    elif kind == "funding":
        if _d(cache_row["funding_rate"]) != _d(raw_row["fundingRate"]):
            mismatches.append("funding_rate")
    elif kind == "ratio":
        for cache_field, raw_field in [("buy_ratio", "buyRatio"), ("sell_ratio", "sellRatio")]:
            if _d(cache_row[cache_field]) != _d(raw_row[raw_field]):
                mismatches.append(cache_field)
    return mismatches


def online_spot_check_symbol(
    symbol: str,
    cache_dir: Path,
    *,
    datasets: Iterable[str],
    samples_per_dataset: int,
    timeout: float,
    retries: int,
    request_pause: float,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    with httpx.Client(base_url=OFFICIAL_API_BASE, timeout=timeout, headers={"User-Agent": "TV-BYBIT-DATA-FIDELITY/1.0"}) as client:
        for name in datasets:
            if name not in ONLINE_SPECS:
                results[name] = {"passed": False, "error": "unsupported online dataset"}
                continue
            endpoint, kind, extra, step_seconds = ONLINE_SPECS[name]
            cache_rows = _rows(cache_dir / f"{name}.json")
            samples: list[dict[str, Any]] = []
            dataset_passed = True
            for idx in _sample_indices(len(cache_rows), samples_per_dataset):
                cache_row = cache_rows[idx]
                time_field = "open_time" if kind in {"candle", "price_candle"} else "source_timestamp"
                ts = _dt(cache_row[time_field])
                ts_ms = int(ts.timestamp() * 1000)
                params: dict[str, Any] = {"category": "linear", "symbol": symbol, "limit": 10, **extra}
                if kind in {"candle", "price_candle"}:
                    params["start"] = ts_ms
                    params["end"] = ts_ms + int((step_seconds or 60) * 1000) - 1
                else:
                    window_ms = int((step_seconds or 60 * 60) * 1000)
                    params["startTime"] = max(0, ts_ms - window_ms)
                    params["endTime"] = ts_ms + window_ms
                try:
                    payload = _raw_get(client, endpoint, params, retries=retries, pause=request_pause)
                    raw_row = _find_raw_list_row(payload, kind=kind, timestamp_ms=ts_ms)
                    if raw_row is None:
                        dataset_passed = False
                        samples.append({"timestamp": ts.isoformat(), "passed": False, "error": "exact timestamp not returned by raw Bybit REST"})
                        continue
                    mismatches = _compare_raw(cache_row, raw_row, kind=kind)
                    if mismatches:
                        dataset_passed = False
                    samples.append({"timestamp": ts.isoformat(), "passed": not mismatches, "mismatched_fields": mismatches})
                except Exception as exc:
                    dataset_passed = False
                    samples.append({"timestamp": ts.isoformat(), "passed": False, "error": repr(exc)})
            results[name] = {"passed": dataset_passed and bool(samples), "samples": samples}
    return {
        "passed": bool(results) and all(row.get("passed") is True for row in results.values()),
        "datasets": results,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    p.add_argument("--symbols", help="comma-separated subset; default all symbols in backfill_summary")
    p.add_argument("--required-5m-coverage", default="1.0", help="required OI and long/short 5m grid coverage, 0..1")
    p.add_argument("--volume-rel-tolerance", default="0.000001", help="max relative mismatch public-trades volume vs 1m kline volume")
    p.add_argument("--online", action="store_true", help="also compare deterministic cache samples to raw Bybit REST responses")
    p.add_argument("--online-datasets", default=",".join(ONLINE_SPECS), help="comma-separated datasets for raw REST spot checks")
    p.add_argument("--samples-per-dataset", type=int, default=1)
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--request-pause", type=float, default=0.12)
    p.add_argument("--output", help="optional report JSON path")
    return p.parse_args()


def main(args: argparse.Namespace) -> dict[str, Any]:
    cache_root = Path(args.cache_root)
    summary = _json(cache_root / "backfill_summary.json")
    summary_results = summary.get("results") or {}
    selected = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else [str(s).upper() for s in summary.get("symbols", [])]
    coverage = Decimal(str(args.required_5m_coverage))
    volume_tol = Decimal(str(args.volume_rel_tolerance))
    if not (Decimal("0") <= coverage <= Decimal("1")):
        raise ValueError("--required-5m-coverage must be in 0..1")
    if volume_tol < 0:
        raise ValueError("--volume-rel-tolerance must be >=0")
    online_datasets = [x.strip() for x in args.online_datasets.split(",") if x.strip()]

    results: dict[str, Any] = {}
    for symbol in selected:
        source = summary_results.get(symbol)
        if not source or source.get("status") != "OK":
            results[symbol] = {"passed": False, "error": "symbol absent or not OK in backfill_summary"}
            continue
        cache_dir_raw = (source.get("manifest") or {}).get("cache_dir")
        if not cache_dir_raw:
            results[symbol] = {"passed": False, "error": "cache_dir absent from manifest"}
            continue
        cache_dir = Path(cache_dir_raw)
        try:
            cache_audit = validate_symbol_cache(cache_dir, required_5m_coverage=coverage, volume_rel_tolerance=volume_tol)
            online_audit = None
            if args.online:
                online_audit = online_spot_check_symbol(
                    symbol,
                    cache_dir,
                    datasets=online_datasets,
                    samples_per_dataset=args.samples_per_dataset,
                    timeout=args.timeout,
                    retries=args.retries,
                    request_pause=args.request_pause,
                )
            passed = cache_audit["passed"] and (online_audit is None or online_audit["passed"])
            results[symbol] = {"passed": passed, "cache": cache_audit, "online": online_audit}
        except Exception as exc:
            results[symbol] = {"passed": False, "error": repr(exc)}

    passed = bool(results) and all(row.get("passed") is True for row in results.values())
    payload = {
        "validator": VERSION,
        "cache_root": str(cache_root),
        "symbols_checked": len(results),
        "online": bool(args.online),
        "passed": passed,
        "source_contract_notes": {
            "kline_volume": "For linear USDT/USDC contracts Bybit documents kline volume in base coin; public trade size is compared in that same quantity unit.",
            "trade_side": "Bybit documents public trade side as taker side Buy/Sell; cached buy/sell taker volumes use that contract.",
            "long_short_timestamp": "Bybit documents a 5min data recording period and timestamp but does not explicitly define start-vs-end availability semantics; raw-value fidelity is validated, availability semantics remains an explicit warning.",
            "orderbook_liquidations": "Not part of extended exact-source audit because historical adapters remain intentionally degraded/unavailable.",
        },
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
