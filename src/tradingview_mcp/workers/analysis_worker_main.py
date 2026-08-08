"""`analysis-worker` service entry point.

Block 1 shipped this as an infra-only stub (start cleanly, report healthy,
expose a metrics port). Block 2 has now implemented every engine module
this worker needs to run a real cycle — `core/analysis/feature_engine.py`,
`regime_classifier.py`, `scoring.py`, `hard_gates.py`, `risk_engine.py`,
the strategies package, and the `core/services/strategy_engine_service.py`
orchestrator that wires them together — all fully unit- and
integration-tested (see `tests/unit/trading/`).

What this file does **not** yet do: poll Postgres for the latest
candles/trade-aggregates/order-book/derivatives/liquidation rows per
symbol, call `fetch_tradingview_context`, and persist the result on a
timer. That DB-polling glue is intentionally deferred rather than shipped
unreviewed — it's the one piece of Block 2 that can only be meaningfully
exercised against a live collector + live Postgres + live TradingView
network access, none of which this sandbox has. Wiring it up is a small,
mechanical step (call `strategy_engine_service.evaluate_symbol` with data
read via the Block 1/2 repositories on a `STRATEGY_INTERVAL_SECONDS`
timer) — flagged as the first thing to do after Block 2 lands on the real
VPS, where it can actually be watched running against live data.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import exposition

from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.observability import metrics as m

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tradingview_mcp.analysis_worker_main")


def _make_health_server(port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/metrics":
                data = exposition.generate_latest(m.REGISTRY)
                self.send_response(200)
                self.send_header("Content-Type", exposition.CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"healthy": true, "note": '
                    b'"Block 2 engine modules are implemented and tested; '
                    b'this worker\'s DB-polling loop wiring is deferred to first VPS run"}'
                )
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


async def main_async() -> None:
    try:
        settings = get_trading_settings()
    except Exception as exc:
        logger.critical("refusing to start: invalid trading configuration: %s", exc)
        sys.exit(1)

    port = settings.metrics_port + 1  # avoid clashing with bybit-collector's port
    server = _make_health_server(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("analysis-worker (Block 1 infra stub) healthy on :%d", port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        logger.debug("analysis-worker heartbeat (no-op until Block 2)")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    server.shutdown()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
