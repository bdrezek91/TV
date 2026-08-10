"""Common execution adapter for BACKTEST_COMPARISON_V1.

The comparison must not let different fill/exit simulators decide which signal
engine wins.  This module translates every candidate into the same neutral W5
paper-execution contract and exposes two explicit policies where production W5
currently has known research quirks:

* entry/exit intrabar sequencing: the live assembly can feed the entry candle
  back into ``simulate_exit``.  A historical OHLC backtest cannot know whether
  a TP/SL happened before or after the fill inside that candle, so the primary
  comparison uses ``NEXT_CANDLE`` (conservative, no intrabar hindsight).
* target schedules: production W5 uses fixed 50/30/20 fractions.  A setup with
  only two targets therefore leaves 20% open after both targets.  The primary
  signal-quality comparison normalises the available target fractions to 100%;
  a ``PRODUCTION_AS_IS`` sensitivity mode preserves the current behaviour.

Nothing here writes paper state, a database, or an exchange endpoint.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from tradingview_mcp.core.analysis import paper_execution_v2 as pe
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, TradeObservation


class TargetSchedulePolicy(str, Enum):
    NORMALIZE_AVAILABLE_TARGETS = "NORMALIZE_AVAILABLE_TARGETS"
    PRODUCTION_AS_IS = "PRODUCTION_AS_IS"


class EntryExitIntrabarPolicy(str, Enum):
    NEXT_CANDLE = "NEXT_CANDLE"
    PRODUCTION_SAME_WINDOW = "PRODUCTION_SAME_WINDOW"


@dataclass(frozen=True)
class ComparisonExecutionResult:
    signal: NeutralSignal
    order: Mapping[str, Any]
    risk_amount: Decimal
    production_pnl_as_is: Decimal
    corrected_net_pnl: Decimal
    r_multiple_production: Optional[Decimal]
    r_multiple_corrected: Optional[Decimal]
    entry_fee_omitted_by_production_pnl: Decimal
    observation: TradeObservation
    target_schedule_policy: TargetSchedulePolicy
    intrabar_policy: EntryExitIntrabarPolicy


def _d(v: Any) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _signal_id(signal: NeutralSignal) -> str:
    raw = "|".join((
        signal.variant.value,
        signal.symbol,
        signal.decision_time.isoformat(),
        signal.setup_name,
        signal.direction,
        str(signal.entry_low), str(signal.entry_high), str(signal.stop_loss),
        ",".join(str(t) for t in signal.targets),
    ))
    return "cmp-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_execution_config(
    target_count: int,
    *,
    base_config: Optional[Mapping[str, Any]] = None,
    target_schedule_policy: TargetSchedulePolicy = TargetSchedulePolicy.NORMALIZE_AVAILABLE_TARGETS,
) -> dict[str, Any]:
    """Return an isolated W5 config for the comparison run.

    For one/two-target setups the normalised policy rescales the production
    50/30/20 weights that actually exist to sum to 1.0.  Three-target setups
    remain exactly 50/30/20.
    """
    if target_count < 1 or target_count > 3:
        raise ValueError("comparison W5 adapter supports 1..3 targets")
    cfg = copy.deepcopy(dict(base_config or pe.DEFAULT_PAPER_CONFIG))
    if target_schedule_policy == TargetSchedulePolicy.PRODUCTION_AS_IS:
        return cfg

    weights = [
        _d(cfg["tp1_close_pct"]),
        _d(cfg["tp2_close_pct"]),
        _d(cfg["tp3_close_pct"]),
    ][:target_count]
    total = sum(weights, Decimal("0"))
    if total <= 0:
        raise ValueError("target close fractions must sum to > 0")
    normalized = [w / total for w in weights]
    for i, w in enumerate(normalized, start=1):
        cfg[f"tp{i}_close_pct"] = w
    return cfg


def neutral_setup_to_w5(signal: NeutralSignal) -> dict[str, Any]:
    signal.validate()
    return {
        "setup_type": signal.setup_name,
        "direction": signal.direction,
        "entry_zone": {"low": signal.entry_low, "high": signal.entry_high},
        "invalidation": signal.metadata.get("invalidation"),
        "stop_loss": signal.stop_loss,
        "targets": list(signal.targets),
        "confidence": signal.metadata.get("confidence", 1.0),
        "price_only_mode": signal.metadata.get("price_only_mode", False),
    }


def build_fixed_risk_decision(
    signal: NeutralSignal,
    *,
    capital: Decimal = Decimal("10000"),
    risk_pct: Decimal = Decimal("1.0"),
) -> dict[str, Any]:
    """Common alpha-test sizing: identical fixed risk for every generator.

    Mirrors W4's conservative worst-case-entry sizing.  This is intentionally
    separate from the full-system/native-W4 comparison.
    """
    signal.validate()
    worst_entry = signal.entry_high if signal.direction == "LONG" else signal.entry_low
    sl_distance = abs(worst_entry - signal.stop_loss)
    if sl_distance <= 0:
        raise ValueError("zero stop distance")
    if signal.direction == "LONG" and signal.stop_loss >= signal.entry_low:
        raise ValueError("LONG stop must be below entry zone")
    if signal.direction == "SHORT" and signal.stop_loss <= signal.entry_high:
        raise ValueError("SHORT stop must be above entry zone")
    risk_amount = capital * risk_pct / Decimal("100")
    qty = risk_amount / sl_distance
    return {
        "decision": "APPROVED",
        "direction": signal.direction,
        "entry_zone": {"low": signal.entry_low, "high": signal.entry_high},
        "stop_loss": signal.stop_loss,
        "targets": list(signal.targets),
        "worst_case_entry": worst_entry,
        "risk_amount": risk_amount,
        "position_size": qty,
        "position_value": qty * worst_entry,
        "config_version": "BACKTEST_FIXED_RISK_V1",
    }


def _closed_1m_after_decision(candles_1m: Sequence[Mapping[str, Any]], decision_time: dt.datetime) -> list[dict[str, Any]]:
    """Candles whose minute interval starts at/after the decision timestamp.

    At a decision exactly on a minute boundary, the candle opening at T is
    future path and may fill an order placed at T.  Candles opening before T
    are excluded even if supplied accidentally by a caller.
    """
    return [dict(c) for c in candles_1m if c["open_time"] >= decision_time]


def _entry_fee(order: Mapping[str, Any], config: Mapping[str, Any]) -> Decimal:
    if not order.get("entry_time") or not order.get("avg_entry_price") or not order.get("filled_size"):
        return Decimal("0")
    notional = _d(order["avg_entry_price"]) * _d(order["filled_size"])
    return notional * _d(config["taker_fee_rate_pct"]) / Decimal("100")


def simulate_neutral_signal(
    signal: NeutralSignal,
    candles_1m: Sequence[Mapping[str, Any]],
    *,
    capital: Decimal = Decimal("10000"),
    risk_pct: Decimal = Decimal("1.0"),
    target_schedule_policy: TargetSchedulePolicy = TargetSchedulePolicy.NORMALIZE_AVAILABLE_TARGETS,
    intrabar_policy: EntryExitIntrabarPolicy = EntryExitIntrabarPolicy.NEXT_CANDLE,
    base_config: Optional[Mapping[str, Any]] = None,
    confirmation_status: str = "CONFIRMED",
) -> ComparisonExecutionResult:
    """Run one signal through the same pure W5 entry/exit primitives.

    The primary comparison deliberately uses ``NEXT_CANDLE`` after an entry
    fill, because OHLC alone cannot order fill vs TP/SL touches inside the same
    one-minute candle.  ``PRODUCTION_SAME_WINDOW`` is retained as an explicit
    sensitivity/reproducer of current live assembly behaviour.
    """
    signal.validate()
    cfg = build_execution_config(
        len(signal.targets), base_config=base_config,
        target_schedule_policy=target_schedule_policy,
    )
    setup = neutral_setup_to_w5(signal)
    risk = build_fixed_risk_decision(signal, capital=capital, risk_pct=risk_pct)
    risk_amount = _d(risk["risk_amount"])
    future = _closed_1m_after_decision(candles_1m, signal.decision_time)

    order = pe.create_order(
        signal.symbol, _signal_id(signal), setup, risk, signal.decision_time, cfg,
        confirmation_status=confirmation_status,
    )
    horizon_now = future[-1]["open_time"] + dt.timedelta(minutes=1) if future else signal.decision_time
    order = pe.simulate_entry(order, future, horizon_now, cfg)

    if order["status"] in ("PARTIALLY_FILLED", "OPEN", "TP1_HIT", "TP2_HIT"):
        if intrabar_policy == EntryExitIntrabarPolicy.NEXT_CANDLE and order.get("entry_time"):
            entry_time = dt.datetime.fromisoformat(order["entry_time"])
            exit_candles = [c for c in future if c["open_time"] > entry_time]
        else:
            exit_candles = future
        order = pe.simulate_exit(order, exit_candles, horizon_now, cfg)

    production_pnl = _d(order.get("realized_pnl", 0))
    omitted_entry_fee = _entry_fee(order, cfg)
    corrected_net = production_pnl - omitted_entry_fee
    r_prod = production_pnl / risk_amount if risk_amount else None
    r_corrected = corrected_net / risk_amount if risk_amount else None

    terminal = order["status"] in pe.TERMINAL_STATES
    filled = bool(order.get("entry_time"))
    observation = TradeObservation(
        variant=signal.variant,
        symbol=signal.symbol,
        decision_time=signal.decision_time,
        setup_name=signal.setup_name,
        direction=signal.direction,
        filled=filled,
        r_multiple=(r_corrected if terminal and filled else None),
        pnl_net=(corrected_net if terminal else Decimal("0")),
        fees=_d(order.get("fees_paid", 0)),
        slippage=_d(order.get("slippage_cost", 0)),
        funding=_d(order.get("funding_paid", 0)),
        mae_r=None,
        mfe_r=None,
    )
    return ComparisonExecutionResult(
        signal=signal,
        order=order,
        risk_amount=risk_amount,
        production_pnl_as_is=production_pnl,
        corrected_net_pnl=corrected_net,
        r_multiple_production=r_prod,
        r_multiple_corrected=r_corrected,
        entry_fee_omitted_by_production_pnl=omitted_entry_fee,
        observation=observation,
        target_schedule_policy=target_schedule_policy,
        intrabar_policy=intrabar_policy,
    )


def production_two_target_residual_fraction(config: Optional[Mapping[str, Any]] = None) -> Decimal:
    """Research diagnostic for the known two-target W5 residual-size quirk."""
    cfg = dict(config or pe.DEFAULT_PAPER_CONFIG)
    closed = _d(cfg["tp1_close_pct"]) + _d(cfg["tp2_close_pct"])
    return max(Decimal("0"), Decimal("1") - closed)
