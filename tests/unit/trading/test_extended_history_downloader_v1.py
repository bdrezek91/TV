from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
from decimal import Decimal

import pytest

from tradingview_mcp.core.backtest.extended_history_downloader_v1 import (
    DownloadConfig,
    OfficialBybitDownloader,
)
from tradingview_mcp.core.backtest.extended_history_v1 import (
    parse_kline_rows,
    parse_open_interest_rows,
)


UTC = dt.timezone.utc


def _ms(ts: dt.datetime) -> str:
    return str(int(ts.timestamp() * 1000))


@pytest.mark.asyncio
async def test_time_pagination_moves_backwards_and_deduplicates(tmp_path):
    start = dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    t0, t1, t2 = start, start + dt.timedelta(minutes=1), start + dt.timedelta(minutes=2)
    calls = []

    async def fake_json(path, params):
        calls.append(dict(params))
        if len(calls) == 1:
            # Bybit returns newest first; parser sorts internally.
            return {"result": {"list": [
                [_ms(t2), "102", "103", "101", "102", "1", "102"],
                [_ms(t1), "101", "102", "100", "101", "1", "101"],
            ]}}
        return {"result": {"list": [
            [_ms(t1), "101", "102", "100", "101", "1", "101"],
            [_ms(t0), "100", "101", "99", "100", "1", "100"],
        ]}}

    config = DownloadConfig(cache_root=tmp_path, request_pause_seconds=0)
    async with OfficialBybitDownloader(config, json_getter=fake_json, bytes_getter=lambda _: None) as dl:  # type: ignore[arg-type]
        rows = await dl._time_paged(
            "/v5/market/kline",
            symbol="BTCUSDT",
            start=t0,
            end=t2,
            limit=2,
            parser=parse_kline_rows,
            extra={"interval": "1"},
        )

    assert [r["open_time"] for r in rows] == [t0, t1, t2]
    assert len(calls) == 2
    assert int(calls[1]["end"]) < int(calls[0]["end"])


@pytest.mark.asyncio
async def test_cursor_pagination_follows_next_page_cursor_without_loop(tmp_path):
    start = dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    t0, t1 = start, start + dt.timedelta(minutes=5)
    calls = []

    async def fake_json(path, params):
        calls.append(dict(params))
        if "cursor" not in params:
            return {"result": {"list": [{"timestamp": _ms(t1), "openInterest": "11"}], "nextPageCursor": "page2"}}
        assert params["cursor"] == "page2"
        return {"result": {"list": [{"timestamp": _ms(t0), "openInterest": "10"}], "nextPageCursor": ""}}

    config = DownloadConfig(cache_root=tmp_path, request_pause_seconds=0)
    async with OfficialBybitDownloader(config, json_getter=fake_json, bytes_getter=lambda _: None) as dl:  # type: ignore[arg-type]
        rows = await dl._cursor_paged(
            "/v5/market/open-interest",
            symbol="BTCUSDT",
            start=t0,
            end=t1,
            limit=2,
            parser=parse_open_interest_rows,
            extra={"intervalTime": "5min"},
        )

    assert [r["source_timestamp"] for r in rows] == [t0, t1]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cached_dataset_is_idempotent_and_skips_loader_second_time(tmp_path):
    calls = 0
    config = DownloadConfig(cache_root=tmp_path, request_pause_seconds=0)
    async with OfficialBybitDownloader(config, json_getter=lambda *_: None, bytes_getter=lambda _: None) as dl:  # type: ignore[arg-type]
        async def loader():
            nonlocal calls
            calls += 1
            return [{"open_time": dt.datetime(2026, 8, 10, tzinfo=UTC), "close": Decimal("100")}]

        first = await dl._cached_dataset(tmp_path, "sample", loader, time_fields=("open_time",))
        second = await dl._cached_dataset(tmp_path, "sample", loader, time_fields=("open_time",))

    assert calls == 1
    assert first == second
    assert second[0]["close"] == Decimal("100")


def _gz_trade_bytes() -> bytes:
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="", write_through=True)
        writer = csv.DictWriter(text, fieldnames=["timestamp", "symbol", "side", "size", "price", "trdMatchID"])
        writer.writeheader()
        writer.writerow({
            "timestamp": "1786320000.0",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "1",
            "price": "60000",
            "trdMatchID": "x",
        })
        text.flush()
        text.detach()
    return raw.getvalue()


@pytest.mark.asyncio
async def test_trade_archive_is_aggregated_cached_and_raw_removed_by_default(tmp_path):
    start = dt.datetime.fromtimestamp(1786320000, tz=UTC)
    end = start + dt.timedelta(minutes=2)
    downloads = 0

    async def fake_bytes(url):
        nonlocal downloads
        downloads += 1
        return _gz_trade_bytes()

    config = DownloadConfig(cache_root=tmp_path, keep_raw_trades=False, request_pause_seconds=0)
    async with OfficialBybitDownloader(config, json_getter=lambda *_: None, bytes_getter=fake_bytes) as dl:  # type: ignore[arg-type]
        root = tmp_path / "symbol"
        first = await dl.trade_archives_1m("BTCUSDT", start, end, root)
        second = await dl.trade_archives_1m("BTCUSDT", start, end, root)

    assert downloads == 1
    assert len(first) == 1
    assert first == second
    assert first[0]["buy_taker_volume"] == Decimal("1")
    assert not list((root / "trades_1m_daily").glob("*.csv.gz"))


@pytest.mark.asyncio
async def test_missing_trade_archive_is_cached_as_missing_and_not_retried(tmp_path):
    start = dt.datetime(2026, 8, 10, tzinfo=UTC)
    end = start + dt.timedelta(minutes=2)
    downloads = 0

    async def missing_bytes(url):
        nonlocal downloads
        downloads += 1
        raise FileNotFoundError(url)

    config = DownloadConfig(cache_root=tmp_path, request_pause_seconds=0)
    async with OfficialBybitDownloader(config, json_getter=lambda *_: None, bytes_getter=missing_bytes) as dl:  # type: ignore[arg-type]
        root = tmp_path / "missing"
        assert await dl.trade_archives_1m("BTCUSDT", start, end, root) == []
        assert await dl.trade_archives_1m("BTCUSDT", start, end, root) == []

    assert downloads == 1
