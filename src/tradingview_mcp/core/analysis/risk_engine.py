"""Risk Engine — fully independent of any LLM/AI/Claude involvement.

Computes position sizing deterministically from the plan's formula:

    risk_amount = account_equity * risk_per_trade_pct
    stop_distance = abs(entry_price - stop_loss)
    position_size = risk_amount / stop_distance

then rounds to the instrument's `qty_step`, applies `max_leverage`, and
accounts for fees/slippage in `max_loss_with_fees`. Also enforces the
"no martingale / no risk increase after a loss / no averaging down / no
duplicate positions" rules via its rejection reasons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import List, Optional


@dataclass
class RiskEngineInput:
    account_equity: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    risk_per_trade_pct: Decimal          # e.g. Decimal("0.25") means 0.25%
    tick_size: Decimal
    qty_step: Decimal
    fee_rate: Decimal                    # e.g. Decimal("0.0006") = 6 bps taker
    slippage: Decimal                    # absolute price units
    max_leverage: Decimal
    min_risk_reward: Decimal
    risk_reward: Optional[Decimal] = None
    # Portfolio-state guards (evaluated here as a final backstop; also
    # checked earlier as hard gates so the reason is never silently lost).
    has_open_position_same_symbol: bool = False
    daily_loss_limit_hit: bool = False
    consecutive_losses: int = 0
    max_consecutive_losses: int = 3
    daily_trades_count: int = 0
    max_trades_per_day: int = 5
    open_positions_count: int = 0
    max_open_positions: int = 1


@dataclass
class RiskEngineResult:
    approved: bool
    risk_amount: Decimal
    position_size: Decimal
    effective_leverage: Decimal
    max_loss_with_fees: Decimal
    rejection_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "risk_amount": str(self.risk_amount),
            "position_size": str(self.position_size),
            "effective_leverage": str(self.effective_leverage),
            "max_loss_with_fees": str(self.max_loss_with_fees),
            "rejection_reasons": self.rejection_reasons,
        }


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def evaluate_risk(inp: RiskEngineInput) -> RiskEngineResult:
    reasons: List[str] = []

    if inp.has_open_position_same_symbol:
        reasons.append("a position already exists for this symbol — no duplicate/averaging positions")

    if inp.daily_loss_limit_hit:
        reasons.append("daily loss limit reached — no new risk taken today")

    if inp.consecutive_losses >= inp.max_consecutive_losses:
        reasons.append(
            f"consecutive-loss limit reached ({inp.consecutive_losses}/{inp.max_consecutive_losses}) "
            "— no risk-increase-after-loss / no martingale"
        )

    if inp.daily_trades_count >= inp.max_trades_per_day:
        reasons.append(f"daily trade limit reached ({inp.daily_trades_count}/{inp.max_trades_per_day})")

    if inp.open_positions_count >= inp.max_open_positions:
        reasons.append(f"max open positions reached ({inp.open_positions_count}/{inp.max_open_positions})")

    if inp.risk_reward is not None and inp.risk_reward < inp.min_risk_reward:
        reasons.append(f"risk/reward {inp.risk_reward} below minimum {inp.min_risk_reward}")

    stop_distance = abs(inp.entry_price - inp.stop_loss)
    if stop_distance <= 0:
        reasons.append("stop distance is zero or negative — cannot size a position")
        return RiskEngineResult(
            approved=False,
            risk_amount=Decimal("0"),
            position_size=Decimal("0"),
            effective_leverage=Decimal("0"),
            max_loss_with_fees=Decimal("0"),
            rejection_reasons=reasons,
        )

    if inp.account_equity <= 0:
        reasons.append("account equity must be positive")
        return RiskEngineResult(
            approved=False,
            risk_amount=Decimal("0"),
            position_size=Decimal("0"),
            effective_leverage=Decimal("0"),
            max_loss_with_fees=Decimal("0"),
            rejection_reasons=reasons,
        )

    risk_amount = (inp.account_equity * inp.risk_per_trade_pct / Decimal("100")).quantize(Decimal("0.01"))
    raw_position_size = risk_amount / stop_distance
    position_size = _round_down_to_step(raw_position_size, inp.qty_step)

    notional = position_size * inp.entry_price
    effective_leverage = (notional / inp.account_equity) if inp.account_equity else Decimal("0")
    effective_leverage = effective_leverage.quantize(Decimal("0.01"))

    if effective_leverage > inp.max_leverage:
        # Scale the position down to respect the leverage cap rather than
        # rejecting outright — this is a sizing constraint, not a setup
        # quality problem.
        capped_notional = inp.account_equity * inp.max_leverage
        position_size = _round_down_to_step(capped_notional / inp.entry_price, inp.qty_step)
        notional = position_size * inp.entry_price
        effective_leverage = (notional / inp.account_equity).quantize(Decimal("0.01")) if inp.account_equity else Decimal("0")

    if position_size <= 0:
        reasons.append("position size rounds down to zero at the configured qty_step — risk too small")

    entry_fee = notional * inp.fee_rate
    exit_notional = position_size * inp.stop_loss
    exit_fee = exit_notional * inp.fee_rate
    slippage_cost = position_size * inp.slippage
    max_loss_with_fees = (
        position_size * stop_distance + entry_fee + exit_fee + slippage_cost
    ).quantize(Decimal("0.01"))

    approved = len(reasons) == 0

    return RiskEngineResult(
        approved=approved,
        risk_amount=risk_amount,
        position_size=position_size,
        effective_leverage=effective_leverage,
        max_loss_with_fees=max_loss_with_fees,
        rejection_reasons=reasons,
    )
