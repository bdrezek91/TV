"""Bybit public WebSocket collector.

Orchestrates: connect -> subscribe -> receive loop -> dispatch to
orderbook/aggregators -> periodic flush to Postgres, with automatic
reconnect (exponential backoff + jitter), heartbeat, resubscribe on
reconnect, staleness tracking, dedup, structured logging, and metrics.

The receive loop and reconnect logic depend only on the `WSTransport`
protocol (see `client.py`), so tests drive it with `FakeWSTransport` loaded
from fixture JSON — no real network in the default test run.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable, Dict, Optional

from tradingview_mcp.core.market_data.bybit.aggregation import (
    LiquidationAggregator,
    TradeAggregator,
    TradeDeduplicator,
)
from tradingview_mcp.core.market_data.bybit.archive import RawDataArchiver
from tradingview_mcp.core.market_data.bybit.client import WSTransport
from tradingview_mcp.core.market_data.bybit.orderbook import LocalOrderBook
from tradingview_mcp.core.market_data.bybit.reconnect import ReconnectState, is_stale
from tradingview_mcp.core.market_data.bybit.schemas import (
    build_subscribe_args,
    parse_klines,
    parse_liquidations,
    parse_orderbook_message,
    parse_ticker,
    parse_trades,
    topic_symbol,
)
from tradingview_mcp.core.observability import metrics as m

logger = logging.getLogger("tradingview_mcp.bybit_collector")

HEARTBEAT_INTERVAL_SECONDS = 20.0


@dataclass
class SymbolState:
    orderbook: LocalOrderBook
    trade_aggregator: TradeAggregator
    liquidation_aggregator: LiquidationAggregator
    dedup: TradeDeduplicator = field(default_factory=TradeDeduplicator)
    last_trade_time: Optional[dt.datetime] = None
    last_orderbook_time: Optional[dt.datetime] = None
    last_ticker: Optional[dict] = None
    last_ticker_time: Optional[dt.datetime] = None
    last_message_time: Optional[dt.datetime] = None


# Callback signatures the collector invokes so persistence stays outside this module.
OnOrderbookSnapshot = Callable[[dict], Awaitable[None]]
OnTradeAggregate = Callable[[dict], Awaitable[None]]
OnLiquidationAggregate = Callable[[dict], Awaitable[None]]
OnKline = Callable[[dict], Awaitable[None]]


class BybitCollector:
    def __init__(
        self,
        transport: WSTransport,
        symbols: list,
        *,
        collect_trades: bool = True,
        orderbook_depth: Optional[int] = 50,
        collect_liquidations: bool = True,
        collect_tickers: bool = True,
        kline_interval: Optional[str] = "1",
        max_data_age_trades_seconds: float = 10,
        max_data_age_orderbook_seconds: float = 5,
        orderbook_snapshot_interval_seconds: float = 5,
        archiver: Optional[RawDataArchiver] = None,
        on_orderbook_snapshot: Optional[OnOrderbookSnapshot] = None,
        on_trade_aggregate: Optional[OnTradeAggregate] = None,
        on_liquidation_aggregate: Optional[OnLiquidationAggregate] = None,
        on_kline: Optional[OnKline] = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self.symbols = symbols
        self.collect_trades = collect_trades
        self.orderbook_depth = orderbook_depth
        self.collect_liquidations = collect_liquidations
        self.collect_tickers = collect_tickers
        self.kline_interval = kline_interval
        self.max_data_age_trades_seconds = max_data_age_trades_seconds
        self.max_data_age_orderbook_seconds = max_data_age_orderbook_seconds
        self.orderbook_snapshot_interval_seconds = orderbook_snapshot_interval_seconds
        self.archiver = archiver
        self.on_orderbook_snapshot = on_orderbook_snapshot
        self.on_trade_aggregate = on_trade_aggregate
        self.on_liquidation_aggregate = on_liquidation_aggregate
        self.on_kline = on_kline
        self._sleep = sleep_fn

        self.state: Dict[str, SymbolState] = {
            sym: SymbolState(
                orderbook=LocalOrderBook(sym),
                trade_aggregator=TradeAggregator(sym),
                liquidation_aggregator=LiquidationAggregator(sym),
            )
            for sym in symbols
        }
        self.reconnect_state = ReconnectState()
        self.connected = False
        self._stopping = False
        self._last_snapshot_flush: Dict[str, dt.datetime] = {}

    # -- Lifecycle -----------------------------------------------------

    async def run_forever(self) -> None:
        """Main loop: connect, subscribe, receive-and-dispatch, and
        reconnect with backoff on any disconnect, until :meth:`stop` is
        called."""
        if self.archiver is not None:
            await self.archiver.start()
        while not self._stopping:
            try:
                await self._connect_and_subscribe()
                await self._receive_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                m.collector_errors_total.labels(component="websocket").inc()
                logger.warning("bybit collector disconnected: %r", exc)
            finally:
                self.connected = False
                for sym in self.symbols:
                    m.bybit_ws_connected.labels(symbol=sym).set(0)
                    self.state[sym].orderbook.force_inconsistent()
                await self.transport.close()

            if self._stopping:
                break

            delay = self.reconnect_state.next_delay()
            for sym in self.symbols:
                m.bybit_reconnects_total.labels(symbol=sym, reason="disconnect").inc()
            logger.info("reconnecting in %.2fs (attempt %d)", delay, self.reconnect_state.attempt)
            await self._sleep(delay)

        if self.archiver is not None:
            await self.archiver.stop()

    async def stop(self) -> None:
        """Graceful shutdown: stop the loop, flush in-flight aggregates."""
        self._stopping = True
        await self.transport.close()

    async def _connect_and_subscribe(self) -> None:
        await self.transport.connect()
        self.connected = True
        self.reconnect_state.reset()
        for sym in self.symbols:
            m.bybit_ws_connected.labels(symbol=sym).set(1)
        args = build_subscribe_args(
            self.symbols,
            trades=self.collect_trades,
            orderbook_depth=self.orderbook_depth,
            liquidations=self.collect_liquidations,
            tickers=self.collect_tickers,
            kline_interval=self.kline_interval,
        )
        await self.transport.send_json({"op": "subscribe", "args": args})
        logger.info("subscribed to %d topics for %s", len(args), self.symbols)

    async def _receive_loop(self) -> None:
        last_heartbeat = dt.datetime.now(dt.timezone.utc)
        while not self._stopping:
            now = dt.datetime.now(dt.timezone.utc)
            if (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS:
                await self.transport.send_json({"op": "ping"})
                last_heartbeat = now

            msg = await self.transport.recv()
            await self._dispatch(msg)

    # -- Dispatch --------------------------------------------------------

    async def _dispatch(self, msg: dict) -> None:
        topic = msg.get("topic")
        if not topic:
            return  # pong / subscribe ack / other control frames

        symbol = topic_symbol(topic)
        state = self.state.get(symbol)
        if state is None:
            return

        now = dt.datetime.now(dt.timezone.utc)
        state.last_message_time = now
        m.bybit_messages_total.labels(symbol=symbol, topic=topic.split(".")[0]).inc()
        if self.archiver is not None:
            self.archiver.record(symbol, topic.split(".")[0], msg)

        try:
            if topic.startswith("publicTrade"):
                await self._handle_trades(symbol, state, msg)
            elif topic.startswith("orderbook"):
                await self._handle_orderbook(symbol, state, msg)
            elif topic.startswith("allLiquidation"):
                await self._handle_liquidations(symbol, state, msg)
            elif topic.startswith("tickers"):
                self._handle_ticker(symbol, state, msg)
            elif topic.startswith("kline"):
                await self._handle_kline(symbol, state, msg)
        except Exception:
            m.collector_errors_total.labels(component="dispatch").inc()
            logger.exception("error handling message for topic=%s", topic)

    async def _handle_trades(self, symbol: str, state: SymbolState, msg: dict) -> None:
        for trade in parse_trades(msg):
            if state.dedup.is_duplicate(trade.trade_id):
                continue
            state.trade_aggregator.add_trade(trade)
            state.last_trade_time = trade.trade_time
        if self.on_trade_aggregate is not None:
            for bucket in state.trade_aggregator.flush_completed():
                await self.on_trade_aggregate(bucket)

    async def _handle_orderbook(self, symbol: str, state: SymbolState, msg: dict) -> None:
        delta = parse_orderbook_message(msg)
        was_consistent = state.orderbook.is_consistent
        applied = state.orderbook.apply(delta)
        state.last_orderbook_time = delta.event_time
        if not applied or (was_consistent and not state.orderbook.is_consistent):
            m.orderbook_consistency_errors_total.labels(symbol=symbol).inc()
            logger.warning("orderbook gap detected for %s (u=%s)", symbol, delta.update_id)

        last_flush = self._last_snapshot_flush.get(symbol)
        now = dt.datetime.now(dt.timezone.utc)
        if last_flush is None or (now - last_flush).total_seconds() >= self.orderbook_snapshot_interval_seconds:
            stats = state.orderbook.stats()
            if stats is not None and self.on_orderbook_snapshot is not None:
                stats["received_at"] = now
                await self.on_orderbook_snapshot(stats)
            self._last_snapshot_flush[symbol] = now

    async def _handle_liquidations(self, symbol: str, state: SymbolState, msg: dict) -> None:
        for liq in parse_liquidations(msg):
            state.liquidation_aggregator.add_liquidation(liq)
        if self.on_liquidation_aggregate is not None:
            for bucket in state.liquidation_aggregator.flush_completed():
                await self.on_liquidation_aggregate(bucket)

    def _handle_ticker(self, symbol: str, state: SymbolState, msg: dict) -> None:
        ticker = parse_ticker(msg)
        state.last_ticker = ticker.__dict__
        state.last_ticker_time = ticker.event_time

    async def _handle_kline(self, symbol: str, state: SymbolState, msg: dict) -> None:
        if self.on_kline is None:
            return
        for kline in parse_klines(msg, symbol):
            if kline.is_closed:
                # upsert_candle()'s "never overwrite newer with older" guard
                # needs source_timestamp. The REST poller's backfill path
                # already uses open_time for this (see rest_poller.py); the
                # live WebSocket path must match so both sources compare on
                # the same clock.
                payload = kline.__dict__.copy()
                payload["source_timestamp"] = kline.open_time
                await self.on_kline(payload)

    # -- Health -------------------------------------------------------

    def is_stale(self, symbol: str) -> bool:
        state = self.state.get(symbol)
        if state is None:
            return True
        trade_stale = is_stale(state.last_trade_time, self.max_data_age_trades_seconds) if self.collect_trades else False
        book_stale = is_stale(state.last_orderbook_time, self.max_data_age_orderbook_seconds)
        return trade_stale or book_stale

    def health_snapshot(self) -> dict:
        out = {}
        for sym, state in self.state.items():
            out[sym] = {
                "connected": self.connected,
                "orderbook_consistent": state.orderbook.is_consistent,
                "is_stale": self.is_stale(sym),
                "last_message_time": state.last_message_time.isoformat() if state.last_message_time else None,
                "reconnect_attempts": self.reconnect_state.total_reconnects,
            }
        return out
