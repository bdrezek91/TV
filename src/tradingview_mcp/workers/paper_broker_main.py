"""`paper-broker` — infrastructure-only stub for Block 1.

Per the plan, Block 1 only needs this service to exist, start cleanly,
report healthy, and expose a metrics port; the real Feature Engine /
Market Regime Classifier / Strategy Engine loop is Block 2 scope.
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
logger = logging.getLogger("tradingview_mcp.paper_broker_main")


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
                self.wfile.write(b'{"healthy": true, "note": "infra-only stub; Block 2 adds real paper-broker logic"}')
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

    port = settings.metrics_port + 2  # avoid clashing with bybit-collector's port
    server = _make_health_server(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("paper-broker (Block 1 infra stub) healthy on :%d", port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        logger.debug("paper-broker heartbeat (no-op until Block 2 (paper trading engine))")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    server.shutdown()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
