from __future__ import annotations

from decimal import Decimal

from tradingview_mcp.core.trading.paper_broker import (
    Bar,
    PaperPositionTracker,
    compute_fee,
    fill_limit_order,
    fill_market_order,
    fill_stop_loss,
    fill_take_profit,
    realized_pnl,
)


def test_market_order_long_fills_at_ask_plus_slippage():
    fill = fill_market_order(
        "LONG", best_bid=Decimal("99.9"), best_ask=Decimal("100.0"),
        quantity=Decimal("1"), slippage=Decimal("0.1"), fee_rate=Decimal("0.0006"),
    )
    assert fill.price == Decimal("100.1")
    assert fill.fee == compute_fee(Decimal("100.1"), Decimal("0.0006"))


def test_market_order_short_fills_at_bid_minus_slippage():
    fill = fill_market_order(
        "SHORT", best_bid=Decimal("99.9"), best_ask=Decimal("100.0"),
        quantity=Decimal("2"), slippage=Decimal("0.1"), fee_rate=Decimal("0.0006"),
    )
    assert fill.price == Decimal("99.8")


def test_limit_order_does_not_fill_on_bare_wick_with_no_liquidity():
    bar = Bar(open=Decimal("101"), high=Decimal("101.5"), low=Decimal("99.5"), close=Decimal("101"),
              sell_taker_volume=Decimal("0"))
    fill = fill_limit_order(
        "LONG", limit_price=Decimal("100"), bar=bar, quantity=Decimal("1"),
        fee_rate=Decimal("0.0006"), min_liquidity=Decimal("1.0"),
    )
    assert fill is None  # wick touched 100 but no real sell-side flow through it


def test_limit_order_fills_when_price_reached_and_liquidity_available():
    bar = Bar(open=Decimal("101"), high=Decimal("101.5"), low=Decimal("99.5"), close=Decimal("100.5"),
              sell_taker_volume=Decimal("5.0"))
    fill = fill_limit_order(
        "LONG", limit_price=Decimal("100"), bar=bar, quantity=Decimal("1"),
        fee_rate=Decimal("0.0006"), min_liquidity=Decimal("1.0"),
    )
    assert fill is not None
    assert fill.price == Decimal("100")


def test_limit_order_never_fills_if_price_not_reached():
    bar = Bar(open=Decimal("101"), high=Decimal("102"), low=Decimal("100.5"), close=Decimal("101.5"),
              sell_taker_volume=Decimal("10"))
    fill = fill_limit_order(
        "LONG", limit_price=Decimal("100"), bar=bar, quantity=Decimal("1"), fee_rate=Decimal("0.0006"),
    )
    assert fill is None


def test_partial_fill_via_close_partial():
    tracker = PaperPositionTracker("LONG", Decimal("100"), Decimal("2"), entry_fee=Decimal("0.12"))
    close1 = tracker.close_partial(Decimal("1"), Decimal("105"), fee_rate=Decimal("0.0006"))
    assert tracker.remaining_quantity == Decimal("1")
    assert close1.pnl > 0
    close2 = tracker.close_partial(Decimal("1"), Decimal("103"), fee_rate=Decimal("0.0006"))
    assert tracker.remaining_quantity == Decimal("0")
    assert tracker.is_closed is True
    assert tracker.total_realized_pnl == close1.pnl + close2.pnl


def test_stop_loss_fills_at_requested_price_when_touched_exactly():
    bar = Bar(open=Decimal("101"), high=Decimal("101.5"), low=Decimal("98"), close=Decimal("99"))
    fill = fill_stop_loss(
        "LONG", stop_price=Decimal("98"), bar=bar, quantity=Decimal("1"),
        slippage=Decimal("0.05"), fee_rate=Decimal("0.0006"),
    )
    assert fill is not None
    assert fill.price == Decimal("98") - Decimal("0.05")


def test_stop_loss_fills_worse_on_gap_through():
    """A fast move gaps straight through the stop — fill uses the bar's
    worse extreme, not the (better) requested stop price."""
    bar = Bar(open=Decimal("101"), high=Decimal("101.5"), low=Decimal("95"), close=Decimal("96"))
    fill = fill_stop_loss(
        "LONG", stop_price=Decimal("98"), bar=bar, quantity=Decimal("1"),
        slippage=Decimal("0.05"), fee_rate=Decimal("0.0006"),
    )
    assert fill.price == Decimal("95") - Decimal("0.05")  # worse than the requested 98


def test_stop_loss_short_direction():
    bar = Bar(open=Decimal("99"), high=Decimal("103"), low=Decimal("98"), close=Decimal("102"))
    fill = fill_stop_loss(
        "SHORT", stop_price=Decimal("101"), bar=bar, quantity=Decimal("1"),
        slippage=Decimal("0.1"), fee_rate=Decimal("0.0006"),
    )
    assert fill.price == Decimal("103") + Decimal("0.1")  # gapped through, worse than requested


def test_stop_loss_not_triggered():
    bar = Bar(open=Decimal("101"), high=Decimal("102"), low=Decimal("99"), close=Decimal("101"))
    fill = fill_stop_loss(
        "LONG", stop_price=Decimal("98"), bar=bar, quantity=Decimal("1"),
        slippage=Decimal("0.05"), fee_rate=Decimal("0.0006"),
    )
    assert fill is None


def test_take_profit_1_and_2_long():
    bar = Bar(open=Decimal("100"), high=Decimal("110"), low=Decimal("99"), close=Decimal("108"))
    tp1 = fill_take_profit("LONG", target_price=Decimal("105"), bar=bar, quantity=Decimal("1"), fee_rate=Decimal("0.0006"))
    tp2 = fill_take_profit("LONG", target_price=Decimal("109"), bar=bar, quantity=Decimal("1"), fee_rate=Decimal("0.0006"))
    assert tp1 is not None and tp1.price == Decimal("105")
    assert tp2 is not None and tp2.price == Decimal("109")


def test_take_profit_not_reached():
    bar = Bar(open=Decimal("100"), high=Decimal("104"), low=Decimal("99"), close=Decimal("101"))
    tp = fill_take_profit("LONG", target_price=Decimal("105"), bar=bar, quantity=Decimal("1"), fee_rate=Decimal("0.0006"))
    assert tp is None


def test_realized_pnl_long_profit_and_loss():
    profit = realized_pnl(
        "LONG", entry_price=Decimal("100"), exit_price=Decimal("110"), quantity=Decimal("1"),
        entry_fee=Decimal("0.06"), exit_fee=Decimal("0.066"),
    )
    assert profit == Decimal("10") - Decimal("0.06") - Decimal("0.066")

    loss = realized_pnl(
        "SHORT", entry_price=Decimal("100"), exit_price=Decimal("110"), quantity=Decimal("1"),
        entry_fee=Decimal("0.06"), exit_fee=Decimal("0.066"),
    )
    assert loss == Decimal("-10") - Decimal("0.06") - Decimal("0.066")


def test_realized_pnl_includes_funding():
    pnl = realized_pnl(
        "LONG", entry_price=Decimal("100"), exit_price=Decimal("105"), quantity=Decimal("1"),
        entry_fee=Decimal("0"), exit_fee=Decimal("0"), funding_paid=Decimal("1.5"),
    )
    assert pnl == Decimal("5") - Decimal("1.5")


def test_position_tracker_applies_funding_signed_by_direction():
    long_tracker = PaperPositionTracker("LONG", Decimal("100"), Decimal("1"), entry_fee=Decimal("0"))
    long_paid = long_tracker.apply_funding(Decimal("0.0001"), Decimal("100"))
    short_tracker = PaperPositionTracker("SHORT", Decimal("100"), Decimal("1"), entry_fee=Decimal("0"))
    short_paid = short_tracker.apply_funding(Decimal("0.0001"), Decimal("100"))
    assert long_paid > 0   # LONG pays positive funding
    assert short_paid < 0  # SHORT receives (negative cost) when funding is positive
