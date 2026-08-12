from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.analysis import paper_execution_v2 as pe


UTC = dt.timezone.utc


def _setup():
    return {
        "direction": "LONG",
        "entry_zone": {"low": Decimal("100"), "high": Decimal("102")},
        "invalidation": Decimal("95"),
        "stop_loss": Decimal("95"),
        "targets": [Decimal("110"), Decimal("120"), Decimal("130")],
    }


def _risk():
    return {
        "decision": "APPROVED",
        "stop_loss": Decimal("95"),
        "targets": [Decimal("110"), Decimal("120"), Decimal("130")],
        "position_size": Decimal("10"),
    }


def _candle(minute: int, low: str, high: str):
    return {
        "open_time": dt.datetime(2026, 8, 10, 10, minute, tzinfo=UTC),
        "open": Decimal("101"),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal("101"),
    }


def test_reproducer_current_w5_second_partial_fill_overwrites_instead_of_accumulating():
    """Documents current production behaviour; this is intentionally not fixed here.

    Two separate shallow touches should be capable of accumulating toward the
    original requested size. Current simulate_entry replaces filled_size and
    remaining_size on the second partial fill, so the first partial fill is
    effectively forgotten. The comparison layer must not silently rely on this
    behaviour when judging strategy quality.
    """
    cfg = dict(pe.DEFAULT_PAPER_CONFIG)
    order = pe.create_order(
        "BTCUSDT", "partial-reproducer", _setup(), _risk(),
        dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC), cfg,
        confirmation_status="CONFIRMED",
    )

    first = pe.simulate_entry(order, [_candle(0, "101.5", "102.5")], dt.datetime(2026, 8, 10, 10, 1, tzinfo=UTC), cfg)
    assert first["status"] == "PARTIALLY_FILLED"
    assert first["filled_size"] == Decimal("5.0")

    second = pe.simulate_entry(first, [_candle(1, "99.5", "100.5")], dt.datetime(2026, 8, 10, 10, 2, tzinfo=UTC), cfg)
    assert second["status"] == "PARTIALLY_FILLED"
    # Reproducer of the current quirk: still 5.0 rather than accumulating to
    # the requested 10.0. If production W5 is deliberately fixed later, this
    # test should be replaced with the corrected invariant.
    assert second["filled_size"] == Decimal("5.0")
    assert second["remaining_size"] == Decimal("5.0")
