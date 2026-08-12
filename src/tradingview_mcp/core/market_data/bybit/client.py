"""Thin transport wrappers around the real network libraries.

Kept deliberately small and behind a `Protocol` so the collector/poller
logic (reconnect, resubscribe, backfill, staleness) can be unit-tested with
a fake transport driven by fixture JSON, with zero real network calls.

WebSocket: implemented directly on top of the `websockets` library (not
`pybit`'s bundled WS client) because the plan requires *our own*
reconnect/backoff/jitter/heartbeat/resubscribe logic to be implemented and
independently testable — wrapping pybit's own reconnect loop would hide
that logic behind an opaque third-party implementation.

REST: implemented on top of the official `pybit` unified-trading HTTP
client, run in a thread (pybit is synchronous) so the async REST poller can
await it without blocking the event loop.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Protocol

BYBIT_WS_PUBLIC_LINEAR_MAINNET = "wss://stream.bybit.com/v5/public/linear"
BYBIT_WS_PUBLIC_LINEAR_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"


class WSTransport(Protocol):
    """Everything the collector needs from a WebSocket connection."""

    async def connect(self) -> None: ...
    async def send_json(self, obj: Dict[str, Any]) -> None: ...
    async def recv(self) -> Dict[str, Any]: ...
    async def close(self) -> None: ...


class BybitWebsocketTransport:
    """Real transport, built on the `websockets` library."""

    def __init__(self, *, testnet: bool = False, url: Optional[str] = None) -> None:
        self.url = url or (
            BYBIT_WS_PUBLIC_LINEAR_TESTNET if testnet else BYBIT_WS_PUBLIC_LINEAR_MAINNET
        )
        self._ws = None

    async def connect(self) -> None:
        import websockets

        self._ws = await websockets.connect(self.url, ping_interval=None, close_timeout=5)

    async def send_json(self, obj: Dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("transport not connected")
        await self._ws.send(json.dumps(obj))

    async def recv(self) -> Dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("transport not connected")
        raw = await self._ws.recv()
        return json.loads(raw)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


class FakeWSTransport:
    """In-memory transport used by tests: replays a fixed list of messages,
    optionally raising a disconnect exception partway through, and records
    every `send_json` call so tests can assert on subscribe/heartbeat
    payloads."""

    def __init__(self, messages: List[Dict[str, Any]], *, disconnect_after: Optional[int] = None) -> None:
        self._messages = list(messages)
        self._disconnect_after = disconnect_after
        self._recv_count = 0
        self.sent: List[Dict[str, Any]] = []
        self.connected = False
        self.connect_count = 0

    async def connect(self) -> None:
        self.connected = True
        self.connect_count += 1

    async def send_json(self, obj: Dict[str, Any]) -> None:
        self.sent.append(obj)

    async def recv(self) -> Dict[str, Any]:
        if self._disconnect_after is not None and self._recv_count >= self._disconnect_after:
            raise ConnectionError("simulated disconnect")
        if not self._messages:
            # Block "forever" (until cancelled) once fixtures are exhausted.
            await asyncio.Event().wait()
        self._recv_count += 1
        return self._messages.pop(0)

    async def close(self) -> None:
        self.connected = False


class BybitRestClient:
    """Wraps `pybit.unified_trading.HTTP` for OI / funding / mark-index /
    long-short-ratio / kline REST endpoints, executed off the event loop
    thread since pybit is a synchronous client."""

    def __init__(self, *, testnet: bool = False) -> None:
        self._testnet = testnet
        self._http = None

    def _client(self):
        if self._http is None:
            from pybit.unified_trading import HTTP

            self._http = HTTP(testnet=self._testnet)
        return self._http

    async def get_open_interest(self, symbol: str, category: str = "linear", interval: str = "5min") -> dict:
        return await asyncio.to_thread(
            self._client().get_open_interest,
            category=category,
            symbol=symbol,
            intervalTime=interval,
        )

    async def get_tickers(self, symbol: str, category: str = "linear") -> dict:
        return await asyncio.to_thread(self._client().get_tickers, category=category, symbol=symbol)

    async def get_funding_rate_history(self, symbol: str, category: str = "linear", limit: int = 1) -> dict:
        return await asyncio.to_thread(
            self._client().get_funding_rate_history,
            category=category,
            symbol=symbol,
            limit=limit,
        )

    async def get_long_short_ratio(self, symbol: str, category: str = "linear", period: str = "5min") -> dict:
        return await asyncio.to_thread(
            self._client().get_long_short_ratio,
            category=category,
            symbol=symbol,
            period=period,
        )

    async def get_kline(
        self, symbol: str, interval: str, category: str = "linear", limit: int = 200, start: Optional[int] = None, end: Optional[int] = None
    ) -> dict:
        kwargs = dict(category=category, symbol=symbol, interval=interval, limit=limit)
        if start is not None:
            kwargs["start"] = start
        if end is not None:
            kwargs["end"] = end
        return await asyncio.to_thread(self._client().get_kline, **kwargs)
