from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from tradingview_mcp.core.market_data.bybit.client import FakeWSTransport
from tradingview_mcp.core.market_data.bybit.websocket_collector import BybitCollector

from .conftest import load_fixture


@pytest.mark.asyncio
async def test_subscribe_sends_expected_topics_on_connect():
    transport = FakeWSTransport(messages=[])
    collector = BybitCollector(transport, ["BTCUSDT"], kline_interval="1")
    await collector._connect_and_subscribe()
    assert transport.connect_count == 1
    assert len(transport.sent) == 1
    args = transport.sent[0]["args"]
    assert "publicTrade.BTCUSDT" in args
    assert "orderbook.50.BTCUSDT" in args
    assert "allLiquidation.BTCUSDT" in args
    assert "tickers.BTCUSDT" in args
    assert "kline.1.BTCUSDT" in args


@pytest.mark.asyncio
async def test_orderbook_snapshot_dispatch_updates_state():
    transport = FakeWSTransport(messages=[load_fixture("orderbook_snapshot.json")])
    received = []

    async def on_snapshot(stats):
        received.append(stats)

    collector = BybitCollector(
        transport,
        ["BTCUSDT"],
        orderbook_snapshot_interval_seconds=0,
        on_orderbook_snapshot=on_snapshot,
    )
    msg = await transport.recv()
    await collector._dispatch(msg)
    assert collector.state["BTCUSDT"].orderbook.is_consistent is True
    assert len(received) == 1
    assert received[0]["is_consistent"] is True


@pytest.mark.asyncio
async def test_kline_dispatch_includes_source_timestamp():
    # Regression test: upsert_candle() requires candle["source_timestamp"]
    # for its "never overwrite newer with older" guard. The live WebSocket
    # path (unlike the REST poller's backfill path) originally omitted it,
    # crashing every closed-candle write with KeyError('source_timestamp').
    transport = FakeWSTransport(messages=[load_fixture("kline.json")])
    received = []

    async def on_kline(candle):
        received.append(candle)

    collector = BybitCollector(
        transport,
        ["BTCUSDT"],
        kline_interval="1",
        on_kline=on_kline,
    )
    msg = await transport.recv()
    await collector._dispatch(msg)
    assert len(received) == 1
    assert "source_timestamp" in received[0]
    assert received[0]["source_timestamp"] == received[0]["open_time"]


@pytest.mark.asyncio
async def test_trade_dedup_across_dispatch():
    transport = FakeWSTransport(messages=[])
    collector = BybitCollector(transport, ["BTCUSDT"])
    msg = load_fixture("public_trades.json")
    await collector._dispatch(msg)
    # 4 raw trades in fixture, 1 duplicate id -> 3 unique counted, once per
    # rolling bucket size (1s/1m/5m) since all fall in the same window.
    bucket = collector.state["BTCUSDT"].trade_aggregator.force_flush_all()
    one_second_buckets = [b for b in bucket if b["bucket_seconds"] == 1]
    assert sum(b["trade_count"] for b in one_second_buckets) == 3


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_and_resubscribes():
    """Simulates: connect -> receive one message -> disconnect -> the
    collector must reconnect and resubscribe (send the subscribe frame a
    second time)."""
    transport = FakeWSTransport(
        messages=[load_fixture("ticker.json")], disconnect_after=1
    )
    collector = BybitCollector(
        transport, ["BTCUSDT"], sleep_fn=lambda _: asyncio.sleep(0)
    )

    task = asyncio.create_task(collector.run_forever())
    # Let it connect, receive the one message, hit the simulated disconnect,
    # back off (instant, since sleep_fn is a no-op), and reconnect+resubscribe.
    for _ in range(50):
        if transport.connect_count >= 2:
            break
        await asyncio.sleep(0.01)

    assert transport.connect_count >= 2
    # Subscribe frame sent once per (re)connect.
    subscribe_frames = [m for m in transport.sent if m.get("op") == "subscribe"]
    assert len(subscribe_frames) >= 2
    assert collector.reconnect_state.total_reconnects >= 1

    await collector.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_staleness_true_before_any_data():
    transport = FakeWSTransport(messages=[])
    collector = BybitCollector(transport, ["BTCUSDT"], max_data_age_orderbook_seconds=5)
    assert collector.is_stale("BTCUSDT") is True


@pytest.mark.asyncio
async def test_staleness_false_after_recent_message():
    transport = FakeWSTransport(messages=[load_fixture("orderbook_snapshot.json")])
    collector = BybitCollector(
        transport, ["BTCUSDT"], max_data_age_orderbook_seconds=3600, collect_trades=False
    )
    msg = await transport.recv()
    await collector._dispatch(msg)
    # The fixture carries a fixed historical timestamp; simulate that the
    # book update just happened "now" to isolate the staleness comparison
    # itself (already exercised end-to-end by the dispatch tests above).
    collector.state["BTCUSDT"].last_orderbook_time = dt.datetime.now(dt.timezone.utc)
    assert collector.is_stale("BTCUSDT") is False


@pytest.mark.asyncio
async def test_orderbook_gap_marks_inconsistent_via_dispatch():
    snapshot = load_fixture("orderbook_snapshot.json")
    deltas = load_fixture("orderbook_deltas.json")
    transport = FakeWSTransport(messages=[snapshot] + deltas)
    collector = BybitCollector(transport, ["BTCUSDT"], orderbook_snapshot_interval_seconds=0)
    for _ in range(4):  # 1 snapshot + 3 deltas
        msg = await transport.recv()
        await collector._dispatch(msg)
    assert collector.state["BTCUSDT"].orderbook.is_consistent is False
