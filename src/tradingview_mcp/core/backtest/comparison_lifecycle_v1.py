"""Lifecycle helpers for research comparison replays.

The signal-quality replay can otherwise count the same symbol/setup repeatedly
at consecutive scan timestamps while an earlier simulated order is still
pending or open. These helpers add a deterministic single-symbol lifecycle
gate without touching production paper state.

The default behaviour remains exactly the V1 behaviour used by the frozen
holdout. A research caller may explicitly enable ``close_target_dust`` to
release a slot at the final TP when every configured target was hit and only a
numerical Decimal rounding remainder is left. This option is intentionally
opt-in so historical V1 outputs remain reproducible byte-for-byte.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional

TERMINAL = {"STOPPED_OUT", "CLOSED", "CANCELLED", "EXPIRED"}
DEFAULT_TARGET_DUST_FRACTION = Decimal("1e-12")


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value
    out = dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out


def _d(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def order_terminal_time(order: Mapping[str, Any], *, fallback_end: dt.datetime) -> dt.datetime:
    """Return when an order stopped occupying its symbol slot under V1 rules.

    W5 records every transition in ``state_history``. For terminal orders we
    use the last terminal transition timestamp. A non-terminal order is
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


def order_effective_terminal_time(
    order: Mapping[str, Any],
    *,
    fallback_end: dt.datetime,
    close_target_dust: bool = False,
    target_dust_fraction: Decimal = DEFAULT_TARGET_DUST_FRACTION,
) -> dt.datetime:
    """Return lifecycle release time with an optional numerical-dust repair.

    ``comparison_execution_v1`` may normalise a two-target 50/30 schedule to
    62.5/37.5. Decimal multiplication of the original filled size can leave a
    microscopic positive ``remaining_size`` after both targets. W5 then keeps
    the order technically open until a much later stop even though 100% of the
    economically meaningful size was closed.

    When explicitly enabled, this helper treats the order as closed at the last
    configured TP if:
    - there was a positive fill;
    - all configured target flags are true; and
    - remaining / filled <= ``target_dust_fraction``.

    A real residual position (for example production-as-is two-target 20%) is
    far above the tolerance and is never reclassified as dust.
    """
    standard = order_terminal_time(order, fallback_end=fallback_end)
    if not close_target_dust:
        return standard
    if target_dust_fraction < 0:
        raise ValueError("target_dust_fraction must be >= 0")

    filled = _d(order.get("filled_size"))
    remaining = _d(order.get("remaining_size"))
    targets = list(order.get("targets") or [])
    flags = list(order.get("tp_hit_flags") or [])
    if filled is None or remaining is None or filled <= 0 or not targets:
        return standard
    if remaining < 0:
        return standard
    if len(flags) < len(targets) or not all(bool(flag) for flag in flags[: len(targets)]):
        return standard
    if remaining / filled > target_dust_fraction:
        return standard

    tp_times: list[dt.datetime] = []
    for row in order.get("state_history", []) or []:
        if str(row.get("status", "")).startswith("TP"):
            ts = _parse_ts(row.get("at"))
            if ts is not None:
                tp_times.append(ts)
    if not tp_times:
        return standard
    return min(standard, max(tp_times))


@dataclass
class SingleSymbolLifecycleGate:
    """Allow at most one pending/open simulated order per symbol at a time.

    ``close_target_dust=False`` is the frozen V1 default. New research that
    explicitly wants the numerical-dust repair must opt in.
    """

    close_target_dust: bool = False
    target_dust_fraction: Decimal = DEFAULT_TARGET_DUST_FRACTION
    busy_until: dict[str, dt.datetime] = field(default_factory=dict)
    suppressed: Counter = field(default_factory=Counter)

    def can_submit(self, symbol: str, decision_time: dt.datetime) -> bool:
        until = self.busy_until.get(symbol)
        if until is None or decision_time >= until:
            return True
        self.suppressed[symbol] += 1
        return False

    def occupy_from_order(self, symbol: str, order: Mapping[str, Any], *, fallback_end: dt.datetime) -> dt.datetime:
        until = order_effective_terminal_time(
            order,
            fallback_end=fallback_end,
            close_target_dust=self.close_target_dust,
            target_dust_fraction=self.target_dust_fraction,
        )
        current = self.busy_until.get(symbol)
        if current is None or until > current:
            self.busy_until[symbol] = until
        return until

    def diagnostics(self) -> dict[str, Any]:
        return {
            "suppressed_signals_total": int(sum(self.suppressed.values())),
            "suppressed_signals_by_symbol": {k: int(v) for k, v in self.suppressed.most_common()},
            "close_target_dust": self.close_target_dust,
            "target_dust_fraction": str(self.target_dust_fraction),
        }
