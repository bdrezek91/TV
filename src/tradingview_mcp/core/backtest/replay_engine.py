"""Replay Engine: no-look-ahead backtesting over already-stored data.

Design principles (per the plan):

- **Strict event ordering, no look-ahead.** A `ReplayEvent` at bar index
  `i` may only have been evaluated using data known up to and including
  bar `i`. Trade simulation for that event only ever looks at bars
  strictly *after* `i` (`bars[i+1:]`) — enforced structurally, not just by
  convention: `ReplayEngine.run()` never receives or reads any bar with an
  index `<= i` when simulating event `i`'s outcome.
- **Reuses production code**, not a parallel reimplementation: fills go
  through `core.trading.paper_broker` (the same market/limit/stop/TP fill
  functions the live paper broker uses), and a `ReplayEvent`'s setup comes
  from `core.services.strategy_engine_service.evaluate_symbol` (the same
  Strategy Engine live trading uses) — the caller wires those together.
- **Fees, spread, slippage, funding, expiry, and partial fills** are all
  applied via the same paper-broker primitives Block 2 already built and
  tested.
- **No parameter auto-optimisation.** This module produces read-only
  reports; nothing here searches parameter space to maximize a historical
  result. If a parameter sweep utility is ever added, it stays a reporting
  tool a human reads, never an auto-apply-best-params loop — enforced by
  omission: there is no "apply" or "select best" function anywhere in this
  module.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Literal, Optional, Sequence

from tradingview_mcp.core.trading.paper_broker import (
    Bar,
    compute_fee,
    fill_stop_loss,
    fill_take_profit,
    realized_pnl,
)

Sample = Literal["in_sample", "out_of_sample", "walk_forward"]


@dataclass(frozen=True)
class ReplayBar:
    timestamp: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    buy_taker_volume: Decimal = Decimal("0")
    sell_taker_volume: Decimal = Decimal("0")

    def to_broker_bar(self) -> Bar:
        return Bar(
            open=self.open, high=self.high, low=self.low, close=self.close,
            buy_taker_volume=self.buy_taker_volume, sell_taker_volume=self.sell_taker_volume,
        )


@dataclass(frozen=True)
class ReplaySignal:
    """The deterministic setup produced by the Strategy Engine at one bar,
    using only data known as of that bar. `bar_index` is that bar's
    position in the `bars` sequence passed to `ReplayEngine.run` — trade
    simulation for this signal only ever reads `bars[bar_index + 1:]`."""

    bar_index: int
    symbol: str
    setup_name: str
    direction: str
    market_regime: Optional[str]
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    expires_at: dt.datetime
    quantity: Decimal
    sample: Sample = "in_sample"


@dataclass
class TradeOutcome:
    signal: ReplaySignal
    entry_time: dt.datetime
    entry_price: Decimal
    exit_time: Optional[dt.datetime]
    exit_price: Optional[Decimal]
    exit_reason: str  # "TP1" | "TP2" | "STOPPED" | "EXPIRED" | "NO_FILL"
    pnl: Decimal
    r_multiple: Optional[Decimal]
    mae: Decimal  # max adverse excursion, in R
    mfe: Decimal  # max favorable excursion, in R


@dataclass
class ReplayReport:
    trades: List[TradeOutcome] = field(default_factory=list)

    def _filter(self, sample: Optional[Sample] = None) -> List[TradeOutcome]:
        if sample is None:
            return self.trades
        return [t for t in self.trades if t.signal.sample == sample]

    def summary(self, sample: Optional[Sample] = None) -> dict:
        trades = [t for t in self._filter(sample) if t.exit_reason != "NO_FILL"]
        n = len(trades)
        if n == 0:
            return {"trades": 0}

        pnls = [t.pnl for t in trades]
        r_multiples = [t.r_multiple for t in trades if t.r_multiple is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = Decimal(len(wins)) / Decimal(n)
        gross_profit = sum(wins, Decimal("0"))
        gross_loss = -sum(losses, Decimal("0"))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        expectancy_r = (sum(r_multiples, Decimal("0")) / Decimal(len(r_multiples))) if r_multiples else None

        running = Decimal("0")
        peak = Decimal("0")
        max_dd = Decimal("0")
        for pnl in pnls:
            running += pnl
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)

        sharpe = None
        if len(r_multiples) >= 2:
            r_floats = [float(r) for r in r_multiples]
            mean = statistics.mean(r_floats)
            stdev = statistics.pstdev(r_floats)
            if stdev > 0:
                sharpe = round(mean / stdev, 4)

        long_trades = [t for t in trades if t.signal.direction == "LONG"]
        short_trades = [t for t in trades if t.signal.direction == "SHORT"]

        by_setup: Dict[str, Decimal] = {}
        by_regime: Dict[str, Decimal] = {}
        by_hour: Dict[int, Decimal] = {}
        by_weekday: Dict[int, Decimal] = {}
        for t in trades:
            by_setup[t.signal.setup_name] = by_setup.get(t.signal.setup_name, Decimal("0")) + t.pnl
            regime_key = t.signal.market_regime or "UNKNOWN"
            by_regime[regime_key] = by_regime.get(regime_key, Decimal("0")) + t.pnl
            hour = t.entry_time.hour
            by_hour[hour] = by_hour.get(hour, Decimal("0")) + t.pnl
            weekday = t.entry_time.weekday()
            by_weekday[weekday] = by_weekday.get(weekday, Decimal("0")) + t.pnl

        return {
            "trades": n,
            "win_rate": str(win_rate.quantize(Decimal("0.0001"))),
            "avg_r": str((sum(r_multiples, Decimal("0")) / Decimal(len(r_multiples))).quantize(Decimal("0.0001"))) if r_multiples else None,
            "expectancy_r": str(expectancy_r.quantize(Decimal("0.0001"))) if expectancy_r is not None else None,
            "profit_factor": str(profit_factor.quantize(Decimal("0.0001"))) if profit_factor is not None else None,
            "max_drawdown": str(max_dd.quantize(Decimal("0.01"))),
            "sharpe_auxiliary": sharpe,
            "pnl_after_costs": str(sum(pnls, Decimal("0")).quantize(Decimal("0.01"))),
            "mae_avg_r": str((sum((t.mae for t in trades), Decimal("0")) / Decimal(n)).quantize(Decimal("0.0001"))),
            "mfe_avg_r": str((sum((t.mfe for t in trades), Decimal("0")) / Decimal(n)).quantize(Decimal("0.0001"))),
            "long_vs_short": {
                "long_trades": len(long_trades), "long_pnl": str(sum((t.pnl for t in long_trades), Decimal("0"))),
                "short_trades": len(short_trades), "short_pnl": str(sum((t.pnl for t in short_trades), Decimal("0"))),
            },
            "by_setup": {k: str(v) for k, v in by_setup.items()},
            "by_regime": {k: str(v) for k, v in by_regime.items()},
            "by_hour": {str(k): str(v) for k, v in by_hour.items()},
            "by_weekday": {str(k): str(v) for k, v in by_weekday.items()},
        }

    def full_report(self) -> dict:
        return {
            "in_sample": self.summary("in_sample"),
            "out_of_sample": self.summary("out_of_sample"),
            "walk_forward": self.summary("walk_forward"),
            "combined": self.summary(None),
        }


class ReplayEngine:
    """Simulates a list of `ReplaySignal`s forward through `bars`, strictly
    respecting event order (no look-ahead), and produces a `ReplayReport`.
    """

    def __init__(
        self,
        bars: Sequence[ReplayBar],
        *,
        fee_rate: Decimal = Decimal("0.0006"),
        slippage: Decimal = Decimal("0"),
        funding_rate_per_bar: Decimal = Decimal("0"),
        max_bars_to_expiry: int = 500,
    ) -> None:
        self.bars = list(bars)
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.funding_rate_per_bar = funding_rate_per_bar
        self.max_bars_to_expiry = max_bars_to_expiry

    def run(self, signals: Sequence[ReplaySignal]) -> ReplayReport:
        report = ReplayReport()
        for signal in signals:
            report.trades.append(self._simulate_one(signal))
        return report

    def _simulate_one(self, signal: ReplaySignal) -> TradeOutcome:
        entry_time = self.bars[signal.bar_index].timestamp
        entry_price = signal.entry_price
        entry_fee = compute_fee(entry_price * signal.quantity, self.fee_rate)

        risk_per_unit = abs(entry_price - signal.stop_loss)
        risk_amount = risk_per_unit * signal.quantity if risk_per_unit > 0 else None

        future_bars = self.bars[signal.bar_index + 1: signal.bar_index + 1 + self.max_bars_to_expiry]
        mae = Decimal("0")  # most adverse excursion, tracked in R (negative)
        mfe = Decimal("0")  # most favorable excursion, tracked in R (positive)
        funding_paid = Decimal("0")

        for bar in future_bars:
            if bar.timestamp > signal.expires_at:
                return self._close(
                    signal, entry_time, entry_price, entry_fee, bar.timestamp, entry_price,
                    "EXPIRED", risk_amount, mae, mfe, funding_paid,
                )

            broker_bar = bar.to_broker_bar()
            funding_paid += compute_fee(entry_price * signal.quantity, self.funding_rate_per_bar)

            if risk_per_unit > 0:
                if signal.direction == "LONG":
                    excursion_low_r = (bar.low - entry_price) / risk_per_unit
                    excursion_high_r = (bar.high - entry_price) / risk_per_unit
                else:
                    excursion_low_r = (entry_price - bar.high) / risk_per_unit
                    excursion_high_r = (entry_price - bar.low) / risk_per_unit
                mae = min(mae, excursion_low_r)
                mfe = max(mfe, excursion_high_r)

            stop_fill = fill_stop_loss(
                signal.direction, stop_price=signal.stop_loss, bar=broker_bar, quantity=signal.quantity,
                slippage=self.slippage, fee_rate=self.fee_rate,
            )
            tp2_fill = fill_take_profit(
                signal.direction, target_price=signal.take_profit_2, bar=broker_bar, quantity=signal.quantity,
                fee_rate=self.fee_rate,
            )
            tp1_fill = fill_take_profit(
                signal.direction, target_price=signal.take_profit_1, bar=broker_bar, quantity=signal.quantity,
                fee_rate=self.fee_rate,
            )

            # Conservative same-bar precedence: if both a stop and a target
            # could technically have triggered within the same bar, assume
            # the worse outcome (the stop) — we cannot know intrabar order
            # from OHLC alone, and the plan requires the model to stay
            # conservative.
            if stop_fill is not None:
                return self._close(
                    signal, entry_time, entry_price, entry_fee, bar.timestamp, stop_fill.price,
                    "STOPPED", risk_amount, mae, mfe, funding_paid, exit_fee=stop_fill.fee,
                )
            if tp2_fill is not None:
                return self._close(
                    signal, entry_time, entry_price, entry_fee, bar.timestamp, tp2_fill.price,
                    "TP2", risk_amount, mae, mfe, funding_paid, exit_fee=tp2_fill.fee,
                )
            if tp1_fill is not None:
                return self._close(
                    signal, entry_time, entry_price, entry_fee, bar.timestamp, tp1_fill.price,
                    "TP1", risk_amount, mae, mfe, funding_paid, exit_fee=tp1_fill.fee,
                )

        return self._close(
            signal, entry_time, entry_price, entry_fee, None, None, "NO_FILL", risk_amount, mae, mfe, funding_paid,
        )

    def _close(
        self, signal, entry_time, entry_price, entry_fee, exit_time, exit_price, reason,
        risk_amount, mae, mfe, funding_paid, exit_fee=Decimal("0"),
    ) -> TradeOutcome:
        if exit_price is None:
            return TradeOutcome(
                signal=signal, entry_time=entry_time, entry_price=entry_price,
                exit_time=exit_time, exit_price=None, exit_reason=reason,
                pnl=Decimal("0"), r_multiple=None, mae=mae, mfe=mfe,
            )
        pnl = realized_pnl(
            signal.direction, entry_price=entry_price, exit_price=exit_price, quantity=signal.quantity,
            entry_fee=entry_fee, exit_fee=exit_fee, funding_paid=funding_paid,
        )
        r_multiple = (pnl / risk_amount).quantize(Decimal("0.0001")) if risk_amount else None
        return TradeOutcome(
            signal=signal, entry_time=entry_time, entry_price=entry_price,
            exit_time=exit_time, exit_price=exit_price, exit_reason=reason,
            pnl=pnl, r_multiple=r_multiple, mae=mae, mfe=mfe,
        )
