"""Local event-driven Paper Trading Engine.

Every function is pure and deterministic: given explicit market inputs
(best bid/ask, a bar's OHLCV, configured fee/slippage/liquidity
parameters) it returns an explicit fill (or `None` if nothing should
fill this bar). No network, no database, no live order placement — this
module cannot place a real order even in principle, it only computes
what a simulated fill would have looked like.

Fill model design notes (per the plan: "can be simplified, but must be
conservative, explicit, deterministic, and testable"):

- Market orders: LONG fills at `best_ask + slippage`, SHORT at
  `best_bid - slippage` — exactly as specified.
- Limit orders: a bar reaching the limit price is necessary but NOT
  sufficient. We additionally require the opposing taker volume in that
  bar to clear a configurable `min_liquidity` threshold — a lone wick
  touch with no real trade flow through the level does not fill. This is
  the simple, explicit, conservative model the plan calls for.
- Stop losses: can fill worse than the requested stop during a fast move
  — if the bar gapped through the stop, the fill uses the worse of
  (stop price, bar's adverse extreme) minus/plus slippage.
- Take profits: filled at the requested price (never *worse* than
  requested — a resting limit-style exit), clipped to the bar's range.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Fill:
    price: Decimal
    quantity: Decimal
    fee: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    buy_taker_volume: Decimal = Decimal("0")
    sell_taker_volume: Decimal = Decimal("0")


def compute_fee(notional: Decimal, fee_rate: Decimal) -> Decimal:
    return (notional * fee_rate).quantize(Decimal("0.00000001"))


def compute_funding(position_notional: Decimal, funding_rate: Decimal) -> Decimal:
    """Positive result = cost paid by a LONG holder (or received by a
    SHORT holder) when `funding_rate` is positive, matching perpetual-swap
    convention. Caller applies the sign based on position direction."""
    return (position_notional * funding_rate).quantize(Decimal("0.00000001"))


def fill_market_order(
    direction: str,
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    quantity: Decimal,
    slippage: Decimal,
    fee_rate: Decimal,
) -> Fill:
    if direction == "LONG":
        price = best_ask + slippage
    elif direction == "SHORT":
        price = best_bid - slippage
    else:
        raise ValueError(f"unknown direction {direction!r}")
    fee = compute_fee(price * quantity, fee_rate)
    return Fill(price=price, quantity=quantity, fee=fee, slippage=slippage)


def fill_limit_order(
    direction: str,
    *,
    limit_price: Decimal,
    bar: Bar,
    quantity: Decimal,
    fee_rate: Decimal,
    min_liquidity: Decimal = Decimal("0"),
) -> Optional[Fill]:
    """Conservative limit fill: requires the bar to have actually reached
    the limit price AND the *opposing* taker flow in that bar to meet
    `min_liquidity` (proxy for "real trade flow was available at that
    level", not just a lone wick touch)."""
    if direction == "LONG":
        reached = bar.low <= limit_price
        available_liquidity = bar.sell_taker_volume  # sellers hitting the bid = fillable liquidity for a resting buy
    elif direction == "SHORT":
        reached = bar.high >= limit_price
        available_liquidity = bar.buy_taker_volume
    else:
        raise ValueError(f"unknown direction {direction!r}")

    if not reached:
        return None
    if available_liquidity < min_liquidity:
        return None

    fee = compute_fee(limit_price * quantity, fee_rate)
    return Fill(price=limit_price, quantity=quantity, fee=fee, slippage=Decimal("0"))


def fill_stop_loss(
    direction: str,
    *,
    stop_price: Decimal,
    bar: Bar,
    quantity: Decimal,
    slippage: Decimal,
    fee_rate: Decimal,
) -> Optional[Fill]:
    """A LONG's stop triggers when the bar's low reaches/breaches
    `stop_price`; fills at `stop_price - slippage`, or worse
    (`bar.low - slippage`) if the bar gapped straight through it. Mirror
    logic for SHORT."""
    if direction == "LONG":
        triggered = bar.low <= stop_price
        if not triggered:
            return None
        worst_price = min(stop_price, bar.low)
        fill_price = worst_price - slippage
    elif direction == "SHORT":
        triggered = bar.high >= stop_price
        if not triggered:
            return None
        worst_price = max(stop_price, bar.high)
        fill_price = worst_price + slippage
    else:
        raise ValueError(f"unknown direction {direction!r}")

    fee = compute_fee(fill_price * quantity, fee_rate)
    return Fill(price=fill_price, quantity=quantity, fee=fee, slippage=slippage)


def fill_take_profit(
    direction: str,
    *,
    target_price: Decimal,
    bar: Bar,
    quantity: Decimal,
    fee_rate: Decimal,
) -> Optional[Fill]:
    """A resting take-profit fills at the requested price the moment the
    bar reaches it — never worse than requested (unlike a stop, a limit-
    style TP does not suffer adverse slippage in this conservative model)."""
    if direction == "LONG":
        triggered = bar.high >= target_price
    elif direction == "SHORT":
        triggered = bar.low <= target_price
    else:
        raise ValueError(f"unknown direction {direction!r}")

    if not triggered:
        return None
    fee = compute_fee(target_price * quantity, fee_rate)
    return Fill(price=target_price, quantity=quantity, fee=fee, slippage=Decimal("0"))


def realized_pnl(
    direction: str,
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    entry_fee: Decimal,
    exit_fee: Decimal,
    funding_paid: Decimal = Decimal("0"),
) -> Decimal:
    """PnL after fees and funding, for a full or partial close of
    `quantity` units. Caller is responsible for tracking remaining
    position size across partial closes (TP1 then TP2, etc.)."""
    if direction == "LONG":
        gross = (exit_price - entry_price) * quantity
    elif direction == "SHORT":
        gross = (entry_price - exit_price) * quantity
    else:
        raise ValueError(f"unknown direction {direction!r}")
    return (gross - entry_fee - exit_fee - funding_paid).quantize(Decimal("0.00000001"))


@dataclass
class PartialClose:
    quantity: Decimal
    exit_price: Decimal
    fee: Decimal
    pnl: Decimal


class PaperPositionTracker:
    """Tracks partial closes (TP1 then TP2, or a stop after TP1) for one
    open paper position, so realized PnL and remaining quantity stay
    consistent across multiple fills."""

    def __init__(self, direction: str, entry_price: Decimal, quantity: Decimal, entry_fee: Decimal) -> None:
        self.direction = direction
        self.entry_price = entry_price
        self.initial_quantity = quantity
        self.remaining_quantity = quantity
        self.entry_fee = entry_fee
        self.closes: list[PartialClose] = []
        self.total_realized_pnl = Decimal("0")
        self.total_fees_paid = entry_fee
        self.total_funding_paid = Decimal("0")

    def close_partial(self, quantity: Decimal, exit_price: Decimal, fee_rate: Decimal) -> PartialClose:
        if quantity > self.remaining_quantity:
            raise ValueError("cannot close more than the remaining position quantity")
        fee = compute_fee(exit_price * quantity, fee_rate)
        # Entry fee is attributed proportionally to the portion being closed.
        proportional_entry_fee = (
            self.entry_fee * quantity / self.initial_quantity if self.initial_quantity else Decimal("0")
        )
        pnl = realized_pnl(
            self.direction,
            entry_price=self.entry_price,
            exit_price=exit_price,
            quantity=quantity,
            entry_fee=proportional_entry_fee,
            exit_fee=fee,
        )
        self.remaining_quantity -= quantity
        self.total_realized_pnl += pnl
        self.total_fees_paid += fee
        close = PartialClose(quantity=quantity, exit_price=exit_price, fee=fee, pnl=pnl)
        self.closes.append(close)
        return close

    def apply_funding(self, funding_rate: Decimal, mark_price: Decimal) -> Decimal:
        notional = self.remaining_quantity * mark_price
        amount = compute_funding(notional, funding_rate)
        # LONG pays positive funding, receives negative funding; SHORT is the mirror.
        signed = amount if self.direction == "LONG" else -amount
        self.total_funding_paid += signed
        return signed

    @property
    def is_closed(self) -> bool:
        return self.remaining_quantity <= 0
