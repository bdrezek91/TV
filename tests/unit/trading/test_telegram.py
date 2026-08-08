from __future__ import annotations

import datetime as dt

import pytest

from tradingview_mcp.core.notifications.telegram import (
    AlertDeduplicator,
    AlertType,
    FakeTelegramTransport,
    build_dedup_key,
    send_alert,
)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def test_dedup_key_shape():
    now = _now()
    key = build_dedup_key("BTCUSDT", "trend_pullback", "LONG", bucket=now)
    assert key.startswith("BTCUSDT|trend_pullback|LONG|")


def test_dedup_key_same_within_bucket_different_across():
    t0 = dt.datetime(2024, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    t1 = dt.datetime(2024, 1, 1, 12, 5, 0, tzinfo=dt.timezone.utc)   # same 15m bucket
    t2 = dt.datetime(2024, 1, 1, 12, 20, 0, tzinfo=dt.timezone.utc)  # next bucket
    k0 = build_dedup_key("BTCUSDT", "trend_pullback", "LONG", bucket=t0, bucket_minutes=15)
    k1 = build_dedup_key("BTCUSDT", "trend_pullback", "LONG", bucket=t1, bucket_minutes=15)
    k2 = build_dedup_key("BTCUSDT", "trend_pullback", "LONG", bucket=t2, bucket_minutes=15)
    assert k0 == k1
    assert k0 != k2


def test_deduplicator_suppresses_within_cooldown():
    dedup = AlertDeduplicator(cooldown_minutes=45)
    now = _now()
    assert dedup.should_send("key1", now=now) is True
    assert dedup.should_send("key1", now=now + dt.timedelta(minutes=10)) is False
    assert dedup.should_send("key1", now=now + dt.timedelta(minutes=46)) is True


def test_deduplicator_different_keys_independent():
    dedup = AlertDeduplicator(cooldown_minutes=45)
    now = _now()
    assert dedup.should_send("key1", now=now) is True
    assert dedup.should_send("key2", now=now) is True


@pytest.mark.asyncio
async def test_send_alert_disabled_by_default_never_calls_transport():
    transport = FakeTelegramTransport()
    dedup = AlertDeduplicator()
    result = await send_alert(
        transport, dedup, enabled=False, chat_id="123", alert_type=AlertType.NEW_SETUP,
        message="test", dedup_key="k1",
    )
    assert result.sent is False
    assert transport.sent == []


@pytest.mark.asyncio
async def test_send_alert_enabled_sends_via_fake_transport_no_real_network():
    transport = FakeTelegramTransport()
    dedup = AlertDeduplicator()
    result = await send_alert(
        transport, dedup, enabled=True, chat_id="123", alert_type=AlertType.TP1,
        message="BTCUSDT trend_pullback hit TP1", dedup_key="k1",
    )
    assert result.sent is True
    assert len(transport.sent) == 1
    assert transport.sent[0][0] == "123"
    assert "[TP1]" in transport.sent[0][1]


@pytest.mark.asyncio
async def test_send_alert_deduplicates_same_setup_across_passes():
    """The same setup surfacing on every analysis pass must not spam —
    exactly the plan's requirement."""
    transport = FakeTelegramTransport()
    dedup = AlertDeduplicator(cooldown_minutes=45)
    now = _now()
    key = build_dedup_key("BTCUSDT", "trend_pullback", "LONG", bucket=now)

    r1 = await send_alert(
        transport, dedup, enabled=True, chat_id="123", alert_type=AlertType.NEW_SETUP,
        message="first pass", dedup_key=key, now=now,
    )
    r2 = await send_alert(
        transport, dedup, enabled=True, chat_id="123", alert_type=AlertType.NEW_SETUP,
        message="second pass, same setup, 10 min later", dedup_key=key, now=now + dt.timedelta(minutes=10),
    )
    assert r1.sent is True
    assert r2.sent is False
    assert r2.suppressed_reason == "deduplicated (cooldown active)"
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_send_alert_resends_after_cooldown_expires():
    transport = FakeTelegramTransport()
    dedup = AlertDeduplicator(cooldown_minutes=45)
    now = _now()
    key = "BTCUSDT|trend_pullback|LONG|fixed"

    await send_alert(transport, dedup, enabled=True, chat_id="1", alert_type=AlertType.NEW_SETUP, message="a", dedup_key=key, now=now)
    r2 = await send_alert(
        transport, dedup, enabled=True, chat_id="1", alert_type=AlertType.NEW_SETUP, message="b",
        dedup_key=key, now=now + dt.timedelta(minutes=46),
    )
    assert r2.sent is True
    assert len(transport.sent) == 2


@pytest.mark.asyncio
async def test_send_alert_never_raises_on_transport_failure():
    transport = FakeTelegramTransport(fail=True)
    dedup = AlertDeduplicator()
    result = await send_alert(
        transport, dedup, enabled=True, chat_id="1", alert_type=AlertType.WORKER_FAILURE,
        message="collector crashed", dedup_key="k",
    )
    assert result.sent is False
    assert result.suppressed_reason == "transport reported failure"


@pytest.mark.asyncio
async def test_send_alert_swallows_transport_exception():
    class RaisingTransport:
        async def send_message(self, chat_id, text):
            raise ConnectionError("network unreachable")

    dedup = AlertDeduplicator()
    result = await send_alert(
        RaisingTransport(), dedup, enabled=True, chat_id="1", alert_type=AlertType.RECONNECT,
        message="reconnected", dedup_key="k",
    )
    assert result.sent is False
    assert "transport raised" in result.suppressed_reason


def test_all_plan_alert_types_exist():
    required = {
        "NEW_SETUP", "SETUP_CANCELLED", "SETUP_EXPIRED", "PAPER_ENTRY",
        "TP1", "TP2", "STOP_LOSS", "DAILY_LOSS_LIMIT", "DATA_LOSS",
        "RECONNECT", "WORKER_FAILURE",
    }
    assert {a.value for a in AlertType} == required
