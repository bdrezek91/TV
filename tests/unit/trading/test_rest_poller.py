from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradingview_mcp.core.market_data.bybit.rest_poller import RestPoller, warmup_status


class FakeRestClient:
    def __init__(self, kline_pages, ticker_response=None, fail_times: int = 0):
        self._kline_pages = list(kline_pages)
        self._ticker_response = ticker_response
        self._fail_times = fail_times
        self._calls = 0

    async def get_tickers(self, symbol, category="linear"):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("simulated failure")
        return self._ticker_response

    async def get_kline(self, symbol, interval, category="linear", limit=200, start=None, end=None):
        self._calls += 1
        if not self._kline_pages:
            return {"result": {"list": []}}
        return {"result": {"list": self._kline_pages.pop(0)}}


def _row(ms: int) -> list:
    return [str(ms), "68000", "68010", "67990", "68005", "1.2", "80000"]


@pytest.mark.asyncio
async def test_warmup_status_insufficient_history():
    status = warmup_status({"1": 5, "15": 5, "60": 5, "240": 5})
    assert status["status"] == "WARMING_UP"
    assert status["reason"] == "insufficient_history"
    assert "15" in status["missing"]


@pytest.mark.asyncio
async def test_warmup_status_ready():
    status = warmup_status({"1": 300, "15": 300, "60": 300, "240": 300})
    assert status["status"] == "READY"


@pytest.mark.asyncio
async def test_derivatives_poll_computes_basis_and_calls_sink():
    ticker_response = {
        "result": {
            "list": [
                {
                    "markPrice": "68000.0",
                    "indexPrice": "67990.0",
                    "openInterest": "125000",
                    "openInterestValue": "8500000000",
                    "fundingRate": "0.0001",
                    "nextFundingTime": "1700028800000",
                }
            ]
        }
    }
    client = FakeRestClient(kline_pages=[], ticker_response=ticker_response)
    sinks = []

    async def sink(snapshot):
        sinks.append(snapshot)

    poller = RestPoller(client, "BTCUSDT", on_derivatives_snapshot=sink)
    result = await poller.poll_derivatives_once()
    assert result is not None
    assert result["mark_price"] == Decimal("68000.0")
    assert result["basis"] == Decimal("10.0")
    assert len(sinks) == 1


@pytest.mark.asyncio
async def test_derivatives_poll_retries_then_succeeds():
    ticker_response = {"result": {"list": [{"markPrice": "1", "indexPrice": "1"}]}}
    client = FakeRestClient(kline_pages=[], ticker_response=ticker_response, fail_times=2)
    poller = RestPoller(client, "BTCUSDT", max_retries=3, sleep_fn=lambda _: _noop())
    result = await poller.poll_derivatives_once()
    assert result is not None


async def _noop():
    return None


@pytest.mark.asyncio
async def test_backfill_candles_is_idempotent_via_sink_dedup():
    """The poller fetches pages; idempotency of the actual write is the
    repository's job (tested separately) — here we verify the poller asks
    for exactly the missing count and stops when the sink reports nothing
    new is needed isn't its concern; it just streams candles to the sink."""
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    page1 = [_row(now_ms - i * 60_000) for i in range(200)]
    page2 = [_row(now_ms - (200 + i) * 60_000) for i in range(10)]
    client = FakeRestClient(kline_pages=[page1, page2])
    seen = []

    async def sink(candle):
        seen.append(candle)

    poller = RestPoller(client, "BTCUSDT", on_candle=sink)
    fetched = await poller.backfill_candles("1", target_count=210, already_have=0)
    assert fetched == 210
    assert len(seen) == 210


@pytest.mark.asyncio
async def test_backfill_skips_when_already_have_enough():
    client = FakeRestClient(kline_pages=[[_row(1)]])
    poller = RestPoller(client, "BTCUSDT")
    fetched = await poller.backfill_candles("15", target_count=200, already_have=200)
    assert fetched == 0
