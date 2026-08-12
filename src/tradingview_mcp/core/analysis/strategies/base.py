from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional


@dataclass(frozen=True)
class StrategySetup:
    """A strategy's proposed trade idea, before scoring/gating/risk. All
    price levels are Decimal and computed deterministically by the
    strategy's own rules — never by an LLM."""

    setup_name: str
    direction: str  # "LONG" / "SHORT"
    entry_min: Decimal
    entry_max: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    invalidation_reason: str
    component_fractions: Dict[str, Decimal] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    experimental: bool = False

    @property
    def entry_reference(self) -> Decimal:
        """A single representative entry price for RR/risk-sizing math —
        the midpoint of the entry zone."""
        return (self.entry_min + self.entry_max) / 2

    def risk_reward(self, target: Decimal) -> Optional[Decimal]:
        entry = self.entry_reference
        risk = abs(entry - self.stop_loss)
        if risk == 0:
            return None
        reward = abs(target - entry)
        return (reward / risk).quantize(Decimal("0.01"))
