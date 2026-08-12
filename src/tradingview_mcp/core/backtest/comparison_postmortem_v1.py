"""Research-only event export helpers for failed Mean Reversion holdout analysis.

This module does not alter strategy decisions, lifecycle semantics, or the
pre-registered holdout verdict.  It only serializes already-computed research
objects into event-level diagnostics suitable for post-mortem analysis after
that holdout has been observed and is no longer pristine.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate

WARSAW = ZoneInfo("Europe/Warsaw")
POSTMORTEM_SCHEMA_VERSION = "MR_V1_POSTMORTEM_EVENT_V1"


def jsonable(value: Any) -> Any:
    """Convert research objects to JSON-safe primitives without losing decimals."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def nested_get(value: Mapping[str, Any] | None, *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def diagnostic_context(chain: Mapping[str, Any], setup_type: str = "mean_reversion") -> dict[str, Any]:
    """Capture point-in-time inputs used by W1-W4, excluding large replay windows."""
    features = chain.get("features") or {}
    return {
        "regime": chain.get("regime") or {},
        "setup": (chain.get("setups") or {}).get(setup_type) or {},
        "confirmation": (chain.get("confirmations") or {}).get(setup_type) or {},
        "risk_decision": (chain.get("risk_decisions") or {}).get(setup_type) or {},
        "data_quality": chain.get("data_quality") or {},
        "w1_classification_data_quality": chain.get("w1_classification_data_quality") or {},
        "features": {
            "tf": features.get("tf") or {},
            "volume": features.get("volume") or {},
            "orderbook": features.get("orderbook") or {},
            "futures": features.get("futures") or {},
            "liquidations": features.get("liquidations") or {},
        },
    }


def build_event_record(
    *,
    schedule: str,
    signal: Any,
    result: Any,
    chain: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one immutable diagnostic record before applying the lifecycle gate."""
    decision_time = signal.decision_time
    return {
        "schema_version": POSTMORTEM_SCHEMA_VERSION,
        "schedule": schedule,
        "symbol": signal.symbol,
        "decision_time": decision_time,
        "decision_time_warsaw": decision_time.astimezone(WARSAW),
        "direction": signal.direction,
        "setup_name": signal.setup_name,
        "signal": {
            "entry_low": signal.entry_low,
            "entry_high": signal.entry_high,
            "stop_loss": signal.stop_loss,
            "targets": list(signal.targets),
            "regime": signal.regime,
            "score": signal.score,
            "metadata": dict(signal.metadata),
        },
        "observation": result.observation,
        "execution": {
            "risk_amount": result.risk_amount,
            "production_pnl_as_is": result.production_pnl_as_is,
            "corrected_net_pnl": result.corrected_net_pnl,
            "r_multiple_production": result.r_multiple_production,
            "r_multiple_corrected": result.r_multiple_corrected,
            "entry_fee_omitted_by_production_pnl": result.entry_fee_omitted_by_production_pnl,
            "target_schedule_policy": result.target_schedule_policy,
            "intrabar_policy": result.intrabar_policy,
            "order": dict(result.order),
        },
        "context": diagnostic_context(chain, signal.setup_name),
        "lifecycle": {
            "status": "UNASSESSED",
            "submitted": None,
            "suppression_reason": None,
        },
    }


def apply_lifecycle_gate(
    events: Sequence[Mapping[str, Any]],
    *,
    decision_end: dt.datetime,
) -> list[dict[str, Any]]:
    """Apply the same per-symbol lifecycle gate and annotate every approved event."""
    gate = SingleSymbolLifecycleGate()
    annotated: list[dict[str, Any]] = []
    for original in events:
        event = dict(original)
        lifecycle = dict(event.get("lifecycle") or {})
        symbol = str(event["symbol"])
        decision_time = event["decision_time"]
        if gate.can_submit(symbol, decision_time):
            lifecycle.update({
                "status": "SUBMITTED",
                "submitted": True,
                "suppression_reason": None,
            })
            order = nested_get(event, "execution", "order") or {}
            gate.occupy_from_order(symbol, order, fallback_end=decision_end)
        else:
            lifecycle.update({
                "status": "SUPPRESSED_BUSY",
                "submitted": False,
                "suppression_reason": "SINGLE_SYMBOL_LIFECYCLE_BUSY",
            })
        event["lifecycle"] = lifecycle
        annotated.append(event)
    return annotated


def flatten_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return analysis-friendly scalar columns while full context stays in JSON."""
    context = event.get("context") or {}
    features = context.get("features") or {}
    tf1h = nested_get(features, "tf", "1h") or {}
    tf15m = nested_get(features, "tf", "15m") or {}
    volume = features.get("volume") or {}
    futures = features.get("futures") or {}
    liquidations = features.get("liquidations") or {}
    confirmation = context.get("confirmation") or {}
    regime = context.get("regime") or {}
    setup = context.get("setup") or {}
    risk = context.get("risk_decision") or {}
    data_quality = context.get("data_quality") or {}
    order = nested_get(event, "execution", "order") or {}
    observation = event.get("observation") or {}
    if is_dataclass(observation):
        observation = asdict(observation)

    state_history = order.get("state_history") or []
    final_state = state_history[-1] if state_history else {}
    exits = order.get("exits") or []
    last_exit = exits[-1] if exits else {}

    row = {
        "schedule": event.get("schedule"),
        "symbol": event.get("symbol"),
        "decision_time": event.get("decision_time"),
        "decision_time_warsaw": event.get("decision_time_warsaw"),
        "warsaw_hour": getattr(event.get("decision_time_warsaw"), "hour", None),
        "direction": event.get("direction"),
        "lifecycle_status": nested_get(event, "lifecycle", "status"),
        "lifecycle_submitted": nested_get(event, "lifecycle", "submitted"),
        "filled": observation.get("filled"),
        "r_multiple": observation.get("r_multiple"),
        "pnl_net": observation.get("pnl_net"),
        "fees": observation.get("fees"),
        "slippage": observation.get("slippage"),
        "funding": observation.get("funding"),
        "order_status": order.get("status"),
        "entry_time": order.get("entry_time"),
        "avg_entry_price": order.get("avg_entry_price"),
        "filled_size": order.get("filled_size"),
        "remaining_size": order.get("remaining_size"),
        "final_state_time": final_state.get("at"),
        "final_state_detail": final_state.get("detail"),
        "last_exit_time": last_exit.get("at") or last_exit.get("time"),
        "last_exit_reason": last_exit.get("reason") or last_exit.get("detail"),
        "entry_low": nested_get(event, "signal", "entry_low"),
        "entry_high": nested_get(event, "signal", "entry_high"),
        "stop_loss": nested_get(event, "signal", "stop_loss"),
        "target_1": (nested_get(event, "signal", "targets") or [None])[0],
        "target_2": ((nested_get(event, "signal", "targets") or []) + [None, None])[1],
        "setup_confidence": setup.get("confidence"),
        "setup_orderflow_confirmed": setup.get("orderflow_confirmed"),
        "setup_price_only_mode": setup.get("price_only_mode"),
        "confirmation_status": confirmation.get("status"),
        "confirmation_confidence": confirmation.get("confidence"),
        "native_risk_decision": risk.get("decision"),
        "regime": regime.get("primary_regime"),
        "regime_confidence": regime.get("confidence"),
        "data_quality_score": data_quality.get("data_quality_score"),
        "position_in_range_1h": tf1h.get("position_in_range"),
        "last_close_1h": tf1h.get("last_close"),
        "atr_1h": tf1h.get("atr"),
        "support_1h": tf1h.get("support"),
        "resistance_1h": tf1h.get("resistance"),
        "ema20_1h": tf1h.get("ema20"),
        "ema50_1h": tf1h.get("ema50"),
        "last_close_15m": tf15m.get("last_close"),
        "atr_15m": tf15m.get("atr"),
        "position_in_range_15m": tf15m.get("position_in_range"),
        "cvd_slope_15m": volume.get("cvd_slope_15m"),
        "volume_zscore": volume.get("volume_zscore"),
        "buy_sell_imbalance": volume.get("buy_sell_imbalance"),
        "oi_change_1h_pct": futures.get("oi_change_1h_pct"),
        "funding_rate": futures.get("funding_rate"),
        "basis_pct": futures.get("basis_pct"),
        "long_short_ratio": futures.get("long_short_ratio"),
        "liq_imbalance_15m": liquidations.get("liq_imbalance_15m"),
        "long_liq_value_15m": liquidations.get("long_liq_value_15m"),
        "short_liq_value_15m": liquidations.get("short_liq_value_15m"),
    }
    return {key: jsonable(value) for key, value in row.items()}
