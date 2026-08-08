from __future__ import annotations

from decimal import Decimal

from tradingview_mcp.core.market_data.bybit.orderbook import LocalOrderBook
from tradingview_mcp.core.market_data.bybit.schemas import parse_orderbook_message

from .conftest import load_fixture


def _snapshot():
    return parse_orderbook_message(load_fixture("orderbook_snapshot.json"))


def _deltas():
    return [parse_orderbook_message(m) for m in load_fixture("orderbook_deltas.json")]


def test_snapshot_populates_book():
    book = LocalOrderBook("BTCUSDT")
    applied = book.apply(_snapshot())
    assert applied is True
    assert book.is_consistent is True
    assert book.best_bid() == (Decimal("68000.0"), Decimal("1.5"))
    assert book.best_ask() == (Decimal("68001.0"), Decimal("1.0"))
    assert book.last_update_id == 1000


def test_delta_adds_new_level():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    d1 = _deltas()[0]
    book.apply(d1)
    assert book.asks[Decimal("68003.0")] == Decimal("0.3")


def test_delta_updates_existing_level():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    book.apply(_deltas()[0])
    assert book.bids[Decimal("68000.0")] == Decimal("2.5")


def test_delta_zero_quantity_removes_level():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    book.apply(_deltas()[0])
    book.apply(_deltas()[1])  # removes 67999.0
    assert Decimal("67999.0") not in book.bids


def test_new_snapshot_resets_book():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    book.apply(_deltas()[0])
    assert len(book.bids) == 3
    book.apply(_snapshot())  # fresh snapshot
    # Back to exactly the 3 original bid levels, none of the delta mutations survive.
    assert book.bids[Decimal("68000.0")] == Decimal("1.5")
    assert len(book.bids) == 3


def test_sequence_gap_detected_and_marks_inconsistent():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    deltas = _deltas()
    book.apply(deltas[0])  # u=1001, ok
    book.apply(deltas[1])  # u=1002, ok
    assert book.is_consistent is True
    applied = book.apply(deltas[2])  # u=1005, gap (expected 1003)
    assert applied is False
    assert book.is_consistent is False
    assert book.gap_count == 1


def test_inconsistent_book_stays_inconsistent_until_new_snapshot():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    deltas = _deltas()
    book.apply(deltas[0])
    book.apply(deltas[1])
    book.apply(deltas[2])  # triggers gap
    assert book.is_consistent is False
    # A further arbitrary delta doesn't magically fix consistency.
    book.apply(deltas[0])
    assert book.is_consistent is False
    # Only a fresh snapshot restores consistency.
    book.apply(_snapshot())
    assert book.is_consistent is True


def test_stats_computed_after_snapshot():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    stats = book.stats()
    assert stats is not None
    assert stats["best_bid"] == Decimal("68000.0")
    assert stats["best_ask"] == Decimal("68001.0")
    assert stats["spread"] == Decimal("1.0")
    assert stats["is_consistent"] is True
    assert "5" in stats["depth_bands"]


def test_stats_none_when_no_book_yet():
    book = LocalOrderBook("BTCUSDT")
    assert book.stats() is None


def test_force_inconsistent_used_on_reconnect():
    book = LocalOrderBook("BTCUSDT")
    book.apply(_snapshot())
    assert book.is_consistent is True
    book.force_inconsistent()
    assert book.is_consistent is False
