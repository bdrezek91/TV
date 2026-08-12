"""In-memory local order book reconstruction from Bybit snapshot + delta
messages, with sequence-gap detection and a consistency flag.

Rules implemented (see plan "Order book"):

- A new snapshot always resets the book.
- A quantity of zero removes a price level.
- An existing level is updated in place; a new level is inserted.
- ``u`` (update id) is tracked and gaps are detected via Bybit's documented
  invariant: for a delta to apply cleanly, its ``u`` must be
  ``previous_u + 1`` (Bybit also allows the same u to repeat harmlessly;
  anything that skips ahead is a gap).
- Once inconsistent, the book stays inconsistent (and thus unusable
  downstream) until a fresh snapshot arrives.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from sortedcontainers import SortedDict
from typing import Dict, List, Optional, Tuple

from tradingview_mcp.core.market_data.bybit.schemas import OrderbookDelta


class OrderBookGapError(Exception):
    """Raised (and caught internally) when a sequence gap is detected."""


class LocalOrderBook:
    """Reconstructs one symbol's order book from a stream of deltas.

    Bids are kept sorted descending (best bid first), asks ascending (best
    ask first). Levels are stored as ``Decimal -> Decimal`` (price -> size).
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: "SortedDict[Decimal, Decimal]" = SortedDict()
        self.asks: "SortedDict[Decimal, Decimal]" = SortedDict()
        self.last_update_id: Optional[int] = None
        self.is_consistent: bool = False  # False until the first snapshot arrives
        self.last_event_time: Optional[dt.datetime] = None
        self.gap_count: int = 0
        self.has_snapshot: bool = False

    # -- Mutation ---------------------------------------------------------

    def apply(self, msg: OrderbookDelta) -> bool:
        """Apply one snapshot or delta message.

        Returns True if applied cleanly, False if a gap was detected (the
        book is marked inconsistent and the caller should trigger a
        resubscribe / fresh snapshot request).
        """
        if msg.msg_type == "snapshot":
            self._reset()
            self._apply_levels(msg.bids, msg.asks)
            self.last_update_id = msg.update_id
            self.is_consistent = True
            self.has_snapshot = True
            self.last_event_time = msg.event_time
            return True

        # delta
        if not self.has_snapshot:
            # Deltas before any snapshot can't be applied meaningfully.
            self.is_consistent = False
            return False

        if self.last_update_id is not None and msg.update_id <= self.last_update_id:
            # Duplicate / out-of-order-but-already-applied delta: harmless, ignore.
            self.last_event_time = msg.event_time
            return True

        if (
            self.last_update_id is not None
            and msg.update_id != self.last_update_id + 1
        ):
            self.gap_count += 1
            self.is_consistent = False
            self.last_event_time = msg.event_time
            return False

        self._apply_levels(msg.bids, msg.asks)
        self.last_update_id = msg.update_id
        self.last_event_time = msg.event_time
        return True

    def _reset(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def _apply_levels(self, bids: List[Tuple[Decimal, Decimal]], asks: List[Tuple[Decimal, Decimal]]) -> None:
        for price, size in bids:
            self._apply_level(self.bids, price, size)
        for price, size in asks:
            self._apply_level(self.asks, price, size)

    @staticmethod
    def _apply_level(book_side: "SortedDict[Decimal, Decimal]", price: Decimal, size: Decimal) -> None:
        if size == 0:
            book_side.pop(price, None)
        else:
            book_side[price] = size

    def force_inconsistent(self) -> None:
        """Explicitly mark the book unusable (e.g. after a reconnect)."""
        self.is_consistent = False

    # -- Read-only views ----------------------------------------------------

    def best_bid(self) -> Optional[Tuple[Decimal, Decimal]]:
        if not self.bids:
            return None
        price = self.bids.peekitem(-1)[0]  # bids sorted ascending by key -> last is highest
        return price, self.bids[price]

    def best_ask(self) -> Optional[Tuple[Decimal, Decimal]]:
        if not self.asks:
            return None
        price = self.asks.peekitem(0)[0]
        return price, self.asks[price]

    def depth(self, side: str, within_bps: Optional[Decimal] = None) -> Decimal:
        """Sum of size on one side, optionally restricted to within N bps of
        the best price on that side."""
        book = self.bids if side == "bid" else self.asks
        if not book:
            return Decimal("0")
        if within_bps is None:
            return sum(book.values(), Decimal("0"))
        best = self.best_bid() if side == "bid" else self.best_ask()
        if best is None:
            return Decimal("0")
        best_price = best[0]
        threshold = best_price * within_bps / Decimal("10000")
        total = Decimal("0")
        for price, size in book.items():
            if side == "bid" and best_price - price <= threshold:
                total += size
            elif side == "ask" and price - best_price <= threshold:
                total += size
        return total

    def stats(self) -> Optional[dict]:
        """Compute the periodic snapshot fields persisted to Postgres.

        Returns None if the book has no bid or no ask (nothing meaningful
        to report yet).
        """
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        best_bid, best_bid_size = bb
        best_ask, best_ask_size = ba
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2
        spread_bps = (spread / mid) * Decimal("10000") if mid else Decimal("0")
        denom = best_bid_size + best_ask_size
        microprice = (
            (best_ask * best_bid_size + best_bid * best_ask_size) / denom
            if denom
            else mid
        )
        bid_depth = self.depth("bid")
        ask_depth = self.depth("ask")
        imbalance = (
            (bid_depth - ask_depth) / (bid_depth + ask_depth)
            if (bid_depth + ask_depth)
            else Decimal("0")
        )
        depth_bands = {}
        for bps in (5, 10, 25, 50):
            depth_bands[str(bps)] = {
                "bid": str(self.depth("bid", within_bps=Decimal(bps))),
                "ask": str(self.depth("ask", within_bps=Decimal(bps))),
            }
        return {
            "symbol": self.symbol,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_bps": spread_bps,
            "mid_price": mid,
            "microprice": microprice,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "imbalance": imbalance,
            "depth_bands": depth_bands,
            "is_consistent": self.is_consistent,
            "source_timestamp": self.last_event_time,
        }
