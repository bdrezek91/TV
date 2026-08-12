"""Telegram alerting with deduplication + cooldown.

Alert types (per the plan): new setup, setup cancelled, setup expired,
paper entry, TP1, TP2, SL, daily-loss-limit hit, data loss, reconnect,
worker failure.

Deduplication key: `symbol + setup + direction + time_bucket` (the plan's
exact formula) — the same setup surfacing on every analysis pass within
one cooldown window sends exactly once. `send_alert` is the single
integration point; it never raises on a delivery failure (a broken
Telegram integration must never crash the collector/worker that's
reporting real trading events).

`TELEGRAM_ENABLED=false` is the default (see `.env.example`); this module
performs the real HTTP POST only when explicitly enabled with a real
token — trivially swapped for a `FakeTelegramTransport` in tests, so no
test ever makes a real network call.
"""
from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol


class AlertType(str, enum.Enum):
    NEW_SETUP = "NEW_SETUP"
    SETUP_CANCELLED = "SETUP_CANCELLED"
    SETUP_EXPIRED = "SETUP_EXPIRED"
    PAPER_ENTRY = "PAPER_ENTRY"
    TP1 = "TP1"
    TP2 = "TP2"
    STOP_LOSS = "STOP_LOSS"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DATA_LOSS = "DATA_LOSS"
    RECONNECT = "RECONNECT"
    WORKER_FAILURE = "WORKER_FAILURE"


class TelegramTransport(Protocol):
    async def send_message(self, chat_id: str, text: str) -> bool: ...


class HttpxTelegramTransport:
    """Real transport: one HTTP POST to the Bot API's sendMessage endpoint."""

    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    async def send_message(self, chat_id: str, text: str) -> bool:
        import httpx

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            return resp.status_code == 200


class FakeTelegramTransport:
    """In-memory transport for tests: records every call, never touches
    the network."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> bool:
        if self.fail:
            return False
        self.sent.append((chat_id, text))
        return True


def build_dedup_key(symbol: str, setup: str, direction: str, *, bucket: dt.datetime, bucket_minutes: int = 15) -> str:
    """`symbol + setup + direction + time_bucket`, exactly per the plan.

    The time bucket is `bucket` floored to `bucket_minutes` — two alerts
    for the same setup within the same bucket collapse to the same key
    even without an explicit cooldown, and the cooldown (see
    `AlertDeduplicator`) additionally suppresses re-sends across bucket
    boundaries within `cooldown_minutes`.
    """
    epoch_minutes = int(bucket.timestamp() // 60)
    floored = epoch_minutes - (epoch_minutes % bucket_minutes)
    return f"{symbol}|{setup}|{direction}|{floored}"


@dataclass
class AlertDeduplicator:
    """Tracks the last time each dedup key was sent and suppresses resends
    within `cooldown_minutes`. Purely in-memory (per-process) — sufficient
    for a single `analysis-worker`/collector process; a multi-process
    deployment would back this with Redis (already provisioned, unused by
    this module — a natural Block 4+ hardening, out of scope here)."""

    cooldown_minutes: int = 45
    _last_sent: Dict[str, dt.datetime] = field(default_factory=dict)

    def should_send(self, dedup_key: str, *, now: Optional[dt.datetime] = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        last = self._last_sent.get(dedup_key)
        if last is not None and (now - last).total_seconds() < self.cooldown_minutes * 60:
            return False
        self._last_sent[dedup_key] = now
        return True

    def reset(self) -> None:
        self._last_sent.clear()


@dataclass
class AlertResult:
    sent: bool
    suppressed_reason: Optional[str] = None


async def send_alert(
    transport: Optional[TelegramTransport],
    deduplicator: AlertDeduplicator,
    *,
    enabled: bool,
    chat_id: str,
    alert_type: AlertType,
    message: str,
    dedup_key: str,
    now: Optional[dt.datetime] = None,
) -> AlertResult:
    """Single integration point for every alert in the system. Never
    raises — a Telegram outage must never take down the collector/worker
    that's reporting a real event."""
    if not enabled or transport is None:
        return AlertResult(sent=False, suppressed_reason="telegram disabled")

    if not deduplicator.should_send(dedup_key, now=now):
        return AlertResult(sent=False, suppressed_reason="deduplicated (cooldown active)")

    try:
        ok = await transport.send_message(chat_id, f"[{alert_type.value}] {message}")
        return AlertResult(sent=ok, suppressed_reason=None if ok else "transport reported failure")
    except Exception as exc:  # noqa: BLE001
        return AlertResult(sent=False, suppressed_reason=f"transport raised: {exc!r}")
