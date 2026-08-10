"""Extended-history primitives for BACKTEST_COMPARISON_V1.

The final 30/90-day replay needs more history than the live Postgres retention.
This module describes/cache-normalises official Bybit sources without touching
production tables. Network orchestration is intentionally separate from the
pure parsers so tests remain hermetic.

Verified source contracts used by the research design (2026-08-10):
- V5 kline: /v5/market/kline, max 1000/page.
- V5 open interest: /v5/market/open-interest, max 200/page.
- V5 funding history: /v5/market/funding/history, max 200/page.
- V5 account ratio: /v5/market/account-ratio, max 500/page.
- public trade archives: https://public.bybit.com/trading/{SYMBOL}/
  with daily files {SYMBOL}YYYY-MM-DD.csv.gz.

Historical orderbook is an official downloadable data product, but this module
does not guess an undocumented archive URL. Until a concrete downloader is
validated, manifests must mark extended orderbook as unavailable/degraded.
Likewise, historical liquidations are never fabricated from price action.
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import gzip
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from tradingview_mcp.core.market_data.bybit.aggregation import TradeAggregator
from tradingview_mcp.core.market_data.bybit.schemas import Trade


OFFICIAL_API_BASE = "https://api.bybit.com"
PUBLIC_TRADE_BASE = "https://public.bybit.com/trading"
DEFAULT_CACHE_ROOT = Path("/app/artifacts/research/backtest_comparison/cache")


@dataclass(frozen=True)
class RestDatasetSpec:
    name: str
    path: str
    page_limit: int
    interval_param: Optional[str] = None
    interval_value: Optional[str] = None


REST_DATASETS = (
    RestDatasetSpec("kline_1m", "/v5/market/kline", 1000, "interval", "1"),
    RestDatasetSpec("kline_15m", "/v5/market/kline", 1000, "interval", "15"),
    RestDatasetSpec("kline_1h", "/v5/market/kline", 1000, "interval", "60"),
    RestDatasetSpec("kline_4h", "/v5/market/kline", 1000, "interval", "240"),
    RestDatasetSpec("mark_5m", "/v5/market/mark-price-kline", 1000, "interval", "5"),
    RestDatasetSpec("index_5m", "/v5/market/index-price-kline", 1000, "interval", "5"),
    RestDatasetSpec("open_interest_5m", "/v5/market/open-interest", 200, "intervalTime", "5min"),
    RestDatasetSpec("funding", "/v5/market/funding/history", 200),
    RestDatasetSpec("long_short_ratio_5m", "/v5/market/account-ratio", 500, "period", "5min"),
)


@dataclass
class ExtendedHistoryManifest:
    symbol: str
    requested_start: dt.datetime
    requested_end: dt.datetime
    cache_version: str = "1"
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)

    def note_source(self, name: str, *, available: bool, rows: int = 0, detail: str = "") -> None:
        self.sources[name] = {"available": bool(available), "rows": int(rows), "detail": detail}

    def finalise_known_gaps(self) -> None:
        if not self.sources.get("liquidations", {}).get("available"):
            if "DEGRADED_NO_HISTORICAL_LIQUIDATIONS" not in self.degradations:
                self.degradations.append("DEGRADED_NO_HISTORICAL_LIQUIDATIONS")
        if not self.sources.get("orderbook", {}).get("available"):
            if "DEGRADED_NO_HISTORICAL_ORDERBOOK" not in self.degradations:
                self.degradations.append("DEGRADED_NO_HISTORICAL_ORDERBOOK")

    def to_dict(self) -> dict[str, Any]:
        self.finalise_known_gaps()
        return {
            "cache_version": self.cache_version,
            "symbol": self.symbol,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "sources": self.sources,
            "degradations": self.degradations,
            "exact_full_source_replay": not self.degradations,
        }


def utc_dates(start: dt.datetime, end: dt.datetime) -> list[dt.date]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    if end < start:
        return []
    first = start.astimezone(dt.timezone.utc).date()
    last = end.astimezone(dt.timezone.utc).date()
    out: list[dt.date] = []
    day = first
    while day <= last:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def public_trade_archive_url(symbol: str, day: dt.date) -> str:
    symbol = symbol.upper().strip()
    if not symbol or not symbol.isalnum():
        raise ValueError("invalid symbol")
    return f"{PUBLIC_TRADE_BASE}/{symbol}/{symbol}{day.isoformat()}.csv.gz"


def public_trade_archive_urls(symbol: str, start: dt.datetime, end: dt.datetime) -> list[str]:
    return [public_trade_archive_url(symbol, d) for d in utc_dates(start, end)]


def rest_query_plan(symbol: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    """Declarative request plan; paginator decides concrete cursors/windows."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out = []
    for spec in REST_DATASETS:
        params: dict[str, Any] = {"category": "linear", "symbol": symbol.upper(), "limit": spec.page_limit}
        if spec.interval_param:
            params[spec.interval_param] = spec.interval_value
        if spec.name == "funding":
            params.update({"startTime": start_ms, "endTime": end_ms})
        elif spec.name in {"open_interest_5m", "long_short_ratio_5m"}:
            params.update({"startTime": start_ms, "endTime": end_ms})
        else:
            params.update({"start": start_ms, "end": end_ms})
        out.append({"name": spec.name, "path": spec.path, "params": params, "page_limit": spec.page_limit})
    return out


def parse_kline_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Trade-price klines, which include volume/turnover."""
    rows = (response.get("result") or {}).get("list") or []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        out.append({
            "open_time": dt.datetime.fromtimestamp(int(row[0]) / 1000, tz=dt.timezone.utc),
            "open": Decimal(str(row[1])),
            "high": Decimal(str(row[2])),
            "low": Decimal(str(row[3])),
            "close": Decimal(str(row[4])),
            "volume": Decimal(str(row[5])),
            "turnover": Decimal(str(row[6])) if len(row) > 6 and row[6] is not None else None,
        })
    out.sort(key=lambda r: r["open_time"])
    return out


def parse_price_kline_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Mark/index/premium price klines; volume is not required."""
    rows = (response.get("result") or {}).get("list") or []
    out = []
    for row in rows:
        if len(row) < 5:
            continue
        out.append({
            "open_time": dt.datetime.fromtimestamp(int(row[0]) / 1000, tz=dt.timezone.utc),
            "open": Decimal(str(row[1])),
            "high": Decimal(str(row[2])),
            "low": Decimal(str(row[3])),
            "close": Decimal(str(row[4])),
        })
    out.sort(key=lambda r: r["open_time"])
    return out


def parse_open_interest_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = (response.get("result") or {}).get("list") or []
    out = []
    for row in rows:
        ts = row.get("timestamp") or row.get("time")
        value = row.get("openInterest")
        if ts is None or value is None:
            continue
        out.append({
            "source_timestamp": dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc),
            "open_interest": Decimal(str(value)),
        })
    out.sort(key=lambda r: r["source_timestamp"])
    return out


def parse_funding_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = (response.get("result") or {}).get("list") or []
    out = []
    for row in rows:
        ts = row.get("fundingRateTimestamp")
        rate = row.get("fundingRate")
        if ts is None or rate is None:
            continue
        out.append({
            "source_timestamp": dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc),
            "funding_rate": Decimal(str(rate)),
        })
    out.sort(key=lambda r: r["source_timestamp"])
    return out


def parse_ratio_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = (response.get("result") or {}).get("list") or []
    out = []
    for row in rows:
        ts = row.get("timestamp")
        buy, sell = row.get("buyRatio"), row.get("sellRatio")
        if ts is None or buy is None or sell is None:
            continue
        buy_d, sell_d = Decimal(str(buy)), Decimal(str(sell))
        ratio = buy_d / sell_d if sell_d else None
        out.append({
            "source_timestamp": dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc),
            "buy_ratio": buy_d,
            "sell_ratio": sell_d,
            "long_short_ratio": ratio,
        })
    out.sort(key=lambda r: r["source_timestamp"])
    return out


def _latest_at_or_before(rows: Sequence[Mapping[str, Any]], timestamps: Sequence[dt.datetime], at: dt.datetime):
    idx = bisect.bisect_right(timestamps, at) - 1
    return rows[idx] if idx >= 0 else None


def reconstruct_derivative_snapshots(
    open_interest: Sequence[Mapping[str, Any]],
    mark_klines: Sequence[Mapping[str, Any]],
    index_klines: Sequence[Mapping[str, Any]],
    funding: Sequence[Mapping[str, Any]],
    ratios: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build 5m-ish derivative snapshots anchored on OI timestamps.

    Every auxiliary value is taken from the latest record at-or-before the OI
    timestamp. Nothing is interpolated from the future.
    """
    oi_rows = sorted(open_interest, key=lambda r: r["source_timestamp"])
    mark_rows = sorted(mark_klines, key=lambda r: r["open_time"])
    index_rows = sorted(index_klines, key=lambda r: r["open_time"])
    funding_rows = sorted(funding, key=lambda r: r["source_timestamp"])
    ratio_rows = sorted(ratios, key=lambda r: r["source_timestamp"])
    mark_ts = [r["open_time"] for r in mark_rows]
    index_ts = [r["open_time"] for r in index_rows]
    funding_ts = [r["source_timestamp"] for r in funding_rows]
    ratio_ts = [r["source_timestamp"] for r in ratio_rows]

    out: list[dict[str, Any]] = []
    for oi in oi_rows:
        ts = oi["source_timestamp"]
        mark = _latest_at_or_before(mark_rows, mark_ts, ts)
        index = _latest_at_or_before(index_rows, index_ts, ts)
        fund = _latest_at_or_before(funding_rows, funding_ts, ts)
        ratio = _latest_at_or_before(ratio_rows, ratio_ts, ts)
        mark_price = Decimal(str(mark["close"])) if mark is not None else None
        index_price = Decimal(str(index["close"])) if index is not None else None
        basis = (mark_price - index_price) if (mark_price is not None and index_price is not None) else None
        basis_pct = (basis / index_price * Decimal("100")) if (basis is not None and index_price) else None
        oi_value = (Decimal(str(oi["open_interest"])) * mark_price) if mark_price is not None else None
        out.append({
            "source_timestamp": ts,
            "open_interest": Decimal(str(oi["open_interest"])),
            "open_interest_value": oi_value,
            "funding_rate": Decimal(str(fund["funding_rate"])) if fund is not None else None,
            "mark_price": mark_price,
            "index_price": index_price,
            "basis": basis,
            "basis_pct": basis_pct,
            "long_short_ratio": Decimal(str(ratio["long_short_ratio"])) if (ratio is not None and ratio.get("long_short_ratio") is not None) else None,
        })
    return out


def _first(row: Mapping[str, str], names: Sequence[str]) -> Optional[str]:
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _parse_trade_timestamp(raw: str) -> dt.datetime:
    value = Decimal(str(raw))
    seconds = value / Decimal("1000") if value >= Decimal("100000000000") else value
    return dt.datetime.fromtimestamp(float(seconds), tz=dt.timezone.utc)


def iter_archive_trades(path: Path, symbol: str) -> Iterable[Trade]:
    """Stream a Bybit daily .csv.gz archive without retaining raw ticks."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return
        for idx, row in enumerate(reader):
            ts = _first(row, ("timestamp", "time", "trade_time", "T"))
            side = _first(row, ("side", "S"))
            size = _first(row, ("size", "qty", "volume", "v"))
            price = _first(row, ("price", "p"))
            if ts is None or side is None or size is None or price is None:
                continue
            side_norm = side.strip().capitalize()
            if side_norm not in {"Buy", "Sell"}:
                continue
            trade_id = _first(row, ("trdMatchID", "execId", "trade_id", "id", "i")) or f"archive-{idx}"
            yield Trade(
                symbol=symbol.upper(),
                side=side_norm,  # type: ignore[arg-type]
                price=Decimal(str(price)),
                size=Decimal(str(size)),
                trade_id=str(trade_id),
                trade_time=_parse_trade_timestamp(ts),
            )


def aggregate_trade_archive_1m(path: Path, symbol: str) -> list[dict[str, Any]]:
    """Aggregate raw public trades with the same live TradeAggregator logic."""
    agg = TradeAggregator(symbol.upper(), bucket_seconds=(60,))
    for trade in iter_archive_trades(path, symbol):
        agg.add_trade(trade)
    rows = agg.force_flush_all()
    rows.sort(key=lambda r: r["bucket_start"])
    return rows


def write_json_cache(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def default(v: Any):
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, (dt.datetime, dt.date)):
            return v.isoformat()
        raise TypeError(type(v).__name__)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=default), encoding="utf-8")
    tmp.replace(path)
