"""Lifecycle helpers for research comparison replays.

The signal-quality replay can otherwise count the same symbol/setup repeatedly
at consecutive scan timestamps while an earlier simulated order is still
pending or open.  These helpers add a deterministic single-symbol lifecycle
gate without touching production paper state.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

TERMINAL = {"STOPPED_OUT", "CLOSED", "CANCELLED", "EXPIRED"}


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value
    out = dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out


def order_terminal_time(order: Mapping[str, Any], *, fallback_end: dt.datetime) -> dt.datetime:
    """Return when an order stopped occupying its symbol slot.

    W5 records every transition in ``state_history``.  For terminal orders we
    use the last terminal transition timestamp.  A non-terminal order is
    conservatively considered active through ``fallback_end``.
    """
    terminal_times: list[dt.datetime] = []
    for row in order.get("state_history", []) or []:
        if str(row.get("status")) in TERMINAL:
            ts = _parse_ts(row.get("at"))
            if ts is not None:
                terminal_times.append(ts)
    if terminal_times:
        return max(terminal_times)
    return fallback_end


@dataclass
class SingleSymbolLifecycleGate:
    """Allow at most one pending/open simulated order per symbol at a time."""

    busy_until: dict[str, dt.datetime] = field(default_factory=dict)
    suppressed: Counter = field(default_factory=Counter)

    def can_submit(self, symbol: str, decision_time: dt.datetime) -> bool:
        until = self.busy_until.get(symbol)
        if until is None or decision_time >= until:
            return True
        self.suppressed[symbol] += 1
        return False

    def occupy_from_order(self, symbol: str, order: Mapping[str, Any], *, fallback_end: dt.datetime) -> dt.datetime:
        until = order_terminal_time(order, fallback_end=fallback_end)
        current = self.busy_until.get(symbol)
        if current is None or until > current:
            self.busy_until[symbol] = until
        return until

    def diagnostics(self) -> dict[str, Any]:
        return {
            "suppressed_signals_total": int(sum(self.suppressed.values())),
            "suppressed_signals_by_symbol": {k: int(v) for k, v in self.suppressed.most_common()},
        }
