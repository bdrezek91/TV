"""`bybit-collector` worker entry point.

Runs the WebSocket collector and the REST poller for every configured
symbol, persists results through the repository layer, and serves
Prometheus metrics + a liveness/health JSON endpoint on
``TRADING_METRICS_PORT`` (default 9100).

Invoked as: ``python -m tradingview_mcp.workers.bybit_collector_main``
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from prometheus_client import exposition

from tradingview_mcp.core.config.trading_settings import TradingSettingsError, get_trading_settings
from tradingview_mcp.core.database.session import dispose_engine, session_scope
from tradingview_mcp.core.database.repositories.market_data_repository import (
    insert_liquidation_aggregate,
    insert_orderbook_snapshot,
    insert_trade_aggregate,
    upsert_candle,
    upsert_derivatives_snapshot,
)
from tradingview_mcp.core.market_data.bybit.archive import RawDataArchiver
from tradingview_mcp.core.market_data.bybit.client import BybitRestClient, BybitWebsocketTransport
from tradingview_mcp.core.market_data.bybit.rest_poller import RestPoller
from tradingview_mcp.core.market_data.bybit.websocket_collector import BybitCollector
from tradingview_mcp.core.observability import metrics as m

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
)
logger = logging.getLogger("tradingview_mcp.bybit_collector_main")


def _make_health_server(collector: BybitCollector, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/metrics":
                data = exposition.generate_latest(m.REGISTRY)
                self.send_response(200)
                self.send_header("Content-Type", exposition.CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/health":
                snapshot = collector.health_snapshot()
                healthy = all(
                    s["connected"] and not s["is_stale"] and s["orderbook_consistent"]
                    for s in snapshot.values()
                ) if snapshot else False
                body = json.dumps({"healthy": healthy, "symbols": snapshot}).encode()
                self.send_response(200 if healthy else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return  # silence default access logging; structured logs above cover us

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    return server


async def _persist_candle(candle: dict) -> None:
    try:
        async with session_scope() as session:
            await upsert_candle(session, candle)
    except Exception:
        m.database_write_errors_total.labels(table="candles").inc()
        logger.exception("failed to persist candle")


async def _persist_trade_aggregate(agg: dict) -> None:
    try:
        async with session_scope() as session:
            await insert_trade_aggregate(session, agg)
    except Exception:
        m.database_write_errors_total.labels(table="trade_aggregates").inc()
        logger.exception("failed to persist trade aggregate")


async def _persist_liquidation_aggregate(agg: dict) -> None:
    try:
        async with session_scope() as session:
            await insert_liquidation_aggregate(session, agg)
    except Exception:
        m.database_write_errors_total.labels(table="liquidation_aggregates").inc()
        logger.exception("failed to persist liquidation aggregate")


async def _persist_orderbook_snapshot(snap: dict) -> None:
    try:
        async with session_scope() as session:
            await insert_orderbook_snapshot(session, snap)
    except Exception:
        m.database_write_errors_total.labels(table="orderbook_feature_snapshots").inc()
        logger.exception("failed to persist orderbook snapshot")


async def _persist_derivatives_snapshot(snap: dict) -> None:
    try:
        async with session_scope() as session:
            await upsert_derivatives_snapshot(session, snap)
    except Exception:
        m.database_write_errors_total.labels(table="derivatives_snapshots").inc()
        logger.exception("failed to persist derivatives snapshot")


async def _run_rest_pollers(settings, stop_event: asyncio.Event) -> None:
    rest_client = BybitRestClient(testnet=settings.bybit_testnet)
    pollers = [
        RestPoller(
            rest_client,
            sym,
            oi_poll_interval_seconds=settings.oi_poll_interval_seconds,
            funding_poll_interval_seconds=settings.funding_poll_interval_seconds,
            on_derivatives_snapshot=_persist_derivatives_snapshot,
            on_candle=_persist_candle,
        )
        for sym in settings.symbols
    ]

    # Startup backfill: best-effort, doesn't block indefinitely.
    for poller, sym in zip(pollers, settings.symbols):
        for interval in ("15", "60", "240"):
            try:
                await poller.backfill_candles(interval, target_count=210)
            except Exception:
                logger.exception("backfill failed for %s interval=%s", sym, interval)

    async def poll_loop(poller: RestPoller) -> None:
        while not stop_event.is_set():
            try:
                await poller.poll_derivatives_once()
            except Exception:
                logger.exception("derivatives poll failed for %s", poller.symbol)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.oi_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    await asyncio.gather(*(poll_loop(p) for p in pollers))


async def main_async() -> None:
    try:
        settings = get_trading_settings()
    except Exception as exc:
        logger.critical("refusing to start: invalid trading configuration: %s", exc)
        sys.exit(1)

    logger.info("starting bybit-collector for symbols=%s testnet=%s", settings.symbols, settings.bybit_testnet)

    transport = BybitWebsocketTransport(testnet=settings.bybit_testnet)
    archiver = RawDataArchiver(
        settings.raw_data_archive_path, enabled=settings.raw_data_archive_enabled
    )

    collector = BybitCollector(
        transport,
        settings.symbols,
        collect_trades=settings.collect_trades,
        orderbook_depth=settings.orderbook_depth if settings.collect_orderbook else None,
        collect_liquidations=settings.collect_liquidations,
        collect_tickers=settings.collect_tickers,
        kline_interval="1" if settings.collect_klines else None,
        max_data_age_trades_seconds=settings.max_data_age_trades_seconds,
        max_data_age_orderbook_seconds=settings.max_data_age_orderbook_seconds,
        orderbook_snapshot_interval_seconds=settings.orderbook_snapshot_interval_seconds,
        archiver=archiver,
        on_orderbook_snapshot=_persist_orderbook_snapshot,
        on_trade_aggregate=_persist_trade_aggregate,
        on_liquidation_aggregate=_persist_liquidation_aggregate,
        on_kline=_persist_candle,
    )

    health_server = _make_health_server(collector, settings.metrics_port)
    health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
    health_thread.start()
    logger.info("health/metrics server listening on :%d", settings.metrics_port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        logger.info("shutdown signal received")
        stop_event.set()
        asyncio.ensure_future(collector.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows dev boxes; not a deployment target

    try:
        await asyncio.gather(
            collector.run_forever(),
            _run_rest_pollers(settings, stop_event),
        )
    finally:
        health_server.shutdown()
        await dispose_engine()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
