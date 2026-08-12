"""Hard gates: reject a setup regardless of its score.

Every one of these is independent of the Scoring Engine — a perfect score
never overrides a failed gate. Each rejection records a precise,
human-readable reason so `strategy_signals.rejection_reasons_json` is
always actionable, per the plan's "every rejected setup is persisted with
full rejection reasons" rule.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

NEVER_TRADEABLE_REGIMES = {"CHAOTIC", "LOW_LIQUIDITY", "NO_DATA"}


@dataclass
class GateContext:
    is_tradeable: bool
    orderbook_consistent: bool
    spread_bps: Optional[Decimal]
    max_spread_bps: Decimal
    entry_price: Optional[Decimal]
    stop_loss: Optional[Decimal]
    risk_reward: Optional[Decimal]
    min_risk_reward: Decimal
    expires_at: Optional[dt.datetime]
    now: dt.datetime
    regime: str
    regime_allowed_for_strategy: bool
    duplicate_active_exists: bool = False
    daily_trades_count: int = 0
    max_trades_per_day: int = 5
    consecutive_losses: int = 0
    max_consecutive_losses: int = 3
    conflicting_position_exists: bool = False
    daily_loss_limit_hit: bool = False


@dataclass
class GateResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)


def evaluate_hard_gates(ctx: GateContext) -> GateResult:
    reasons: List[str] = []

    if not ctx.is_tradeable:
        reasons.append("data is stale or a required source is missing (is_tradeable=false)")

    if not ctx.orderbook_consistent:
        reasons.append("order book is inconsistent (sequence gap not yet resolved by a fresh snapshot)")

    if ctx.regime in NEVER_TRADEABLE_REGIMES:
        reasons.append(f"market regime {ctx.regime} is never tradeable")
    elif not ctx.regime_allowed_for_strategy:
        reasons.append(f"market regime {ctx.regime} is not in this strategy's allowed-regimes list")

    if ctx.spread_bps is not None and ctx.spread_bps > ctx.max_spread_bps:
        reasons.append(f"spread {ctx.spread_bps} bps exceeds limit {ctx.max_spread_bps} bps")

    if ctx.entry_price is None:
        reasons.append("missing entry price")

    if ctx.stop_loss is None:
        reasons.append("missing stop loss")

    if ctx.risk_reward is None:
        reasons.append("risk/reward could not be computed (missing entry/stop/target)")
    elif ctx.risk_reward < ctx.min_risk_reward:
        reasons.append(f"risk/reward {ctx.risk_reward} is below the minimum {ctx.min_risk_reward}")

    if ctx.expires_at is not None and ctx.expires_at <= ctx.now:
        reasons.append("setup has already expired")

    if ctx.duplicate_active_exists:
        reasons.append("an active duplicate signal already exists for this symbol/setup/direction")

    if ctx.daily_trades_count >= ctx.max_trades_per_day:
        reasons.append(f"daily trade limit reached ({ctx.daily_trades_count}/{ctx.max_trades_per_day})")

    if ctx.consecutive_losses >= ctx.max_consecutive_losses:
        reasons.append(
            f"consecutive-loss limit reached ({ctx.consecutive_losses}/{ctx.max_consecutive_losses})"
        )

    if ctx.conflicting_position_exists:
        reasons.append("a conflicting open position already exists for this symbol")

    if ctx.daily_loss_limit_hit:
        reasons.append("daily loss limit has been reached; trading locked for the rest of the day")

    return GateResult(passed=(len(reasons) == 0), reasons=reasons)
