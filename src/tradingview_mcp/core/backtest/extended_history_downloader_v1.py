"""Network/cache orchestration for BACKTEST_COMPARISON_V1 extended history.

Research-only: writes exclusively below the comparison cache root. It never
writes production Postgres tables or paper state and never calls any order
endpoint.

Pagination follows the official V5 contracts:
- kline/mark/index: page backwards with ``end``;
- funding: page backwards with ``endTime``;
- open-interest and account-ratio: follow ``nextPageCursor``.

Public trade archives are streamed day-by-day directly to disk, immediately
aggregated through the live TradeAggregator into 1m buckets, and raw .csv.gz
files are removed by default after successful aggregation.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

from tradingview_mcp.core.backtest.extended_history_v1 import (
    DEFAULT_CACHE_ROOT,
    OFFICIAL_API_BASE,
    ExtendedHistoryManifest,
    aggregate_trade_archive_1m,
    parse_funding_rows,
    parse_kline_rows,
    parse_open_interest_rows,
    parse_price_kline_rows,
    parse_ratio_rows,
    public_trade_archive_url,
    reconstruct_derivative_snapshots,
    utc_dates,
    write_json_cache,
)


JsonGetter = Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]]
BytesGetter = Callable[[str], Awaitable[bytes]]


class ArchiveNotFoundError(FileNotFoundError):
    """A daily public-trade archive does not exist (HTTP 404)."""


@dataclass(frozen=True)
class DownloadConfig:
    cache_root: Path = DEFAULT_CACHE_ROOT
    timeout_seconds: float = 30.0
    max_retries: int = 4
    base_backoff_seconds: float = 0.8
    keep_raw_trades: bool = False
    request_pause_seconds: float = 0.05


def cache_key(symbol: str, start: dt.datetime, end: dt.datetime) -> str:
    s = start.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    e = end.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{symbol.upper()}_{s}_{e}"


def symbol_cache_dir(config: DownloadConfig, symbol: str, start: dt.datetime, end: dt.datetime) -> Path:
    return config.cache_root / cache_key(symbol, start, end)


def _decode_cached(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _retime_row(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    out = dict(row)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str):
            try:
                out[field] = dt.datetime.fromisoformat(value)
            except ValueError:
                pass
    for k, v in list(out.items()):
        if k in fields:
            continue
        if isinstance(v, str):
            try:
                out[k] = Decimal(v)
            except Exception:
                pass
    return out


def load_cached_rows(path: Path, *, time_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = _decode_cached(path)
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    return [_retime_row(dict(r), time_fields) for r in rows]


class OfficialBybitDownloader:
    def __init__(
        self,
        config: DownloadConfig = DownloadConfig(),
        *,
        json_getter: Optional[JsonGetter] = None,
        bytes_getter: Optional[BytesGetter] = None,
    ) -> None:
        self.config = config
        self._injected_json_getter = json_getter
        self._injected_bytes_getter = bytes_getter
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        if self._injected_json_getter is None or self._injected_bytes_getter is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds),
                headers={"User-Agent": "TV-BACKTEST_COMPARISON_V1/1.0"},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._injected_json_getter is not None:
            return await self._injected_json_getter(path, params)
        if self._client is None:
            raise RuntimeError("downloader must be used as async context manager")
        url = OFFICIAL_API_BASE + path
        last: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                response = await self._client.get(url, params=dict(params))
                response.raise_for_status()
                payload = response.json()
                if int(payload.get("retCode", 0)) != 0:
                    raise RuntimeError(f"Bybit retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}")
                return payload
            except Exception as exc:  # noqa: BLE001 - bounded retry at network boundary
                last = exc
                if attempt + 1 >= self.config.max_retries:
                    break
                delay = self.config.base_backoff_seconds * (2 ** attempt) + random.random() * 0.2
                await asyncio.sleep(delay)
        raise RuntimeError(f"Bybit GET failed after retries: {path}: {last!r}")

    async def _download_archive_to_file(self, url: str, destination: Path) -> None:
        """Stream archive to a temp file; never retain a full daily dump in RAM."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        if self._injected_bytes_getter is not None:
            try:
                tmp.write_bytes(await self._injected_bytes_getter(url))
                tmp.replace(destination)
                return
            except FileNotFoundError as exc:
                tmp.unlink(missing_ok=True)
                raise ArchiveNotFoundError(url) from exc

        if self._client is None:
            raise RuntimeError("downloader must be used as async context manager")
        last: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code == 404:
                        raise ArchiveNotFoundError(url)
                    response.raise_for_status()
                    with tmp.open("wb") as fh:
                        async for chunk in response.aiter_bytes():
                            fh.write(chunk)
                tmp.replace(destination)
                return
            except ArchiveNotFoundError:
                tmp.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
                last = exc
                if attempt + 1 >= self.config.max_retries:
                    break
                delay = self.config.base_backoff_seconds * (2 ** attempt) + random.random() * 0.2
                await asyncio.sleep(delay)
        raise RuntimeError(f"archive download failed after retries: {url}: {last!r}")

    async def _pause(self) -> None:
        if self.config.request_pause_seconds > 0:
            await asyncio.sleep(self.config.request_pause_seconds)

    async def _time_paged(
        self,
        path: str,
        *,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        parser,
        extra: Mapping[str, Any],
        end_param: str = "end",
        start_param: Optional[str] = "start",
        timestamp_field: str = "open_time",
    ) -> list[dict[str, Any]]:
        start_ms = int(start.timestamp() * 1000)
        current_end = int(end.timestamp() * 1000)
        dedup: dict[dt.datetime, dict[str, Any]] = {}
        previous_oldest: int | None = None
        while current_end >= start_ms:
            params: dict[str, Any] = {"category": "linear", "symbol": symbol.upper(), "limit": limit, **dict(extra)}
            if start_param:
                params[start_param] = start_ms
            params[end_param] = current_end
            payload = await self._json(path, params)
            page = parser(payload)
            if not page:
                break
            for row in page:
                ts = row[timestamp_field]
                if start <= ts <= end:
                    dedup[ts] = row
            oldest_ts = min(row[timestamp_field] for row in page)
            oldest_ms = int(oldest_ts.timestamp() * 1000)
            if previous_oldest is not None and oldest_ms >= previous_oldest:
                raise RuntimeError(f"pagination made no backward progress for {path} {symbol}")
            previous_oldest = oldest_ms
            if oldest_ms <= start_ms or len(page) < limit:
                break
            current_end = oldest_ms - 1
            await self._pause()
        return [dedup[k] for k in sorted(dedup)]

    async def _cursor_paged(
        self,
        path: str,
        *,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        parser,
        extra: Mapping[str, Any],
        timestamp_field: str = "source_timestamp",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol.upper(),
            "limit": limit,
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            **dict(extra),
        }
        cursor: str | None = None
        seen_cursors: set[str] = set()
        dedup: dict[dt.datetime, dict[str, Any]] = {}
        while True:
            call_params = dict(params)
            if cursor:
                call_params["cursor"] = cursor
            payload = await self._json(path, call_params)
            page = parser(payload)
            for row in page:
                ts = row[timestamp_field]
                if start <= ts <= end:
                    dedup[ts] = row
            next_cursor = str((payload.get("result") or {}).get("nextPageCursor") or "")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(f"cursor pagination loop for {path} {symbol}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            await self._pause()
        return [dedup[k] for k in sorted(dedup)]

    async def klines(self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str) -> list[dict[str, Any]]:
        return await self._time_paged(
            "/v5/market/kline", symbol=symbol, start=start, end=end,
            limit=1000, parser=parse_kline_rows, extra={"interval": interval},
        )

    async def price_klines(self, path: str, symbol: str, start: dt.datetime, end: dt.datetime, interval: str = "5") -> list[dict[str, Any]]:
        if path not in {"/v5/market/mark-price-kline", "/v5/market/index-price-kline"}:
            raise ValueError("unsupported price-kline endpoint")
        return await self._time_paged(
            path, symbol=symbol, start=start, end=end,
            limit=1000, parser=parse_price_kline_rows, extra={"interval": interval},
        )

    async def funding(self, symbol: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        return await self._time_paged(
            "/v5/market/funding/history", symbol=symbol, start=start, end=end,
            limit=200, parser=parse_funding_rows, extra={},
            end_param="endTime", start_param="startTime", timestamp_field="source_timestamp",
        )

    async def open_interest(self, symbol: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        return await self._cursor_paged(
            "/v5/market/open-interest", symbol=symbol, start=start, end=end,
            limit=200, parser=parse_open_interest_rows, extra={"intervalTime": "5min"},
        )

    async def long_short_ratio(self, symbol: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        return await self._cursor_paged(
            "/v5/market/account-ratio", symbol=symbol, start=start, end=end,
            limit=500, parser=parse_ratio_rows, extra={"period": "5min"},
        )

    async def _cached_dataset(
        self,
        root: Path,
        name: str,
        loader: Callable[[], Awaitable[list[dict[str, Any]]]],
        *,
        time_fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        path = root / f"{name}.json"
        if path.exists():
            return load_cached_rows(path, time_fields=time_fields)
        rows = await loader()
        write_json_cache(path, {"dataset": name, "rows": rows})
        return rows

    async def trade_archives_1m(self, symbol: str, start: dt.datetime, end: dt.datetime, root: Path) -> list[dict[str, Any]]:
        daily_root = root / "trades_1m_daily"
        daily_root.mkdir(parents=True, exist_ok=True)
        combined: list[dict[str, Any]] = []
        for day in utc_dates(start, end):
            day_json = daily_root / f"{day.isoformat()}.json"
            if day_json.exists():
                combined.extend(load_cached_rows(day_json, time_fields=("bucket_start",)))
                continue
            url = public_trade_archive_url(symbol, day)
            raw_path = daily_root / f"{day.isoformat()}.csv.gz"
            try:
                if not raw_path.exists():
                    await self._download_archive_to_file(url, raw_path)
                rows = aggregate_trade_archive_1m(raw_path, symbol)
                rows = [r for r in rows if start <= r["bucket_start"] <= end]
                write_json_cache(day_json, {"source_url": url, "rows": rows})
                combined.extend(rows)
                if not self.config.keep_raw_trades:
                    raw_path.unlink(missing_ok=True)
            except ArchiveNotFoundError:
                write_json_cache(day_json, {"source_url": url, "rows": [], "missing": True})
                raw_path.unlink(missing_ok=True)
            await self._pause()
        dedup = {r["bucket_start"]: r for r in combined}
        return [dedup[k] for k in sorted(dedup)]

    async def download_symbol(self, symbol: str, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("start/end must be timezone-aware and end > start")
        symbol = symbol.upper().strip()
        root = symbol_cache_dir(self.config, symbol, start, end)
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"

        c1m = await self._cached_dataset(root, "kline_1m", lambda: self.klines(symbol, start, end, "1"), time_fields=("open_time",))
        c15 = await self._cached_dataset(root, "kline_15m", lambda: self.klines(symbol, start, end, "15"), time_fields=("open_time",))
        c1h = await self._cached_dataset(root, "kline_1h", lambda: self.klines(symbol, start, end, "60"), time_fields=("open_time",))
        c4h = await self._cached_dataset(root, "kline_4h", lambda: self.klines(symbol, start, end, "240"), time_fields=("open_time",))
        mark = await self._cached_dataset(root, "mark_5m", lambda: self.price_klines("/v5/market/mark-price-kline", symbol, start, end), time_fields=("open_time",))
        index = await self._cached_dataset(root, "index_5m", lambda: self.price_klines("/v5/market/index-price-kline", symbol, start, end), time_fields=("open_time",))
        oi = await self._cached_dataset(root, "open_interest_5m", lambda: self.open_interest(symbol, start, end), time_fields=("source_timestamp",))
        funding = await self._cached_dataset(root, "funding", lambda: self.funding(symbol, start, end), time_fields=("source_timestamp",))
        ratio = await self._cached_dataset(root, "long_short_ratio_5m", lambda: self.long_short_ratio(symbol, start, end), time_fields=("source_timestamp",))
        trades = await self.trade_archives_1m(symbol, start, end, root)

        deriv = reconstruct_derivative_snapshots(oi, mark, index, funding, ratio)
        write_json_cache(root / "derivatives_reconstructed.json", {"dataset": "derivatives_reconstructed", "rows": deriv})

        manifest = ExtendedHistoryManifest(symbol, start, end)
        for name, rows in (
            ("kline_1m", c1m), ("kline_15m", c15), ("kline_1h", c1h), ("kline_4h", c4h),
            ("mark_5m", mark), ("index_5m", index), ("open_interest_5m", oi),
            ("funding", funding), ("long_short_ratio_5m", ratio),
            ("trades_1m", trades), ("derivatives", deriv),
        ):
            manifest.note_source(name, available=bool(rows), rows=len(rows))
        manifest.note_source("liquidations", available=False, detail="no validated official historical liquidation adapter")
        manifest.note_source("orderbook", available=False, detail="official historical download product exists; concrete archive adapter not yet validated")
        payload = manifest.to_dict()
        payload["cache_dir"] = str(root)
        write_json_cache(manifest_path, payload)
        return payload
