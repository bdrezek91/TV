"""Research-only semantic-target candidate for BACKTEST_COMPARISON_V1.

This variant keeps the decision-time W1 de-bias and stop/invalidation geometry
repairs from ``comparison_candidate_v1`` but deliberately preserves each
setup's original semantic targets.  Instead of forcing TP1 to >= 1.5R, W4's
research gate may pass a setup when the *weighted target profile* clears the
same 1.5R threshold.

Why: a staged-exit strategy can legitimately take a first partial profit below
1.5R while later targets make the complete payoff profile acceptable.  Moving a
mean-reversion TP1 away from the range midpoint merely to satisfy W4 changes the
strategy thesis.  This module tests the cleaner alternative without touching
production W4.
"""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.analysis import setup_detector_v2 as sd
from tradingview_mcp.core.backtest.comparison_candidate_v1 import (
    classification_data_quality_for_degraded_replay,
    repair_setup_geometry,
)
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import RESEARCH_HIST_MAX_AGES
from tradingview_mcp.core.backtest.comparison_v1 import Variant
from tradingview_mcp.core.services import regime_service_v2 as rs


_TARGET_WEIGHTS = (Decimal("0.50"), Decimal("0.30"), Decimal("0.20"))
_RR_REJECTION_PREFIX = "reward:risk "


def _d(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def repair_setup_geometry_preserve_targets(setup: Mapping[str, Any], features: Mapping[str, Any]) -> dict[str, Any]:
    """Repair SL/invalidation geometry but restore original semantic targets."""
    out = repair_setup_geometry(setup, features)
    original_targets = copy.deepcopy(list(setup.get("targets") or []))
    out["targets"] = original_targets
    adjustments = [
        a for a in (out.get("comparison_candidate_adjustments") or [])
        if a != "TARGETS_ALIGNED_TO_W4_WORST_ENTRY_R_MULTIPLES"
    ]
    if original_targets and out.get("direction") in {"LONG", "SHORT"}:
        adjustments.append("SEMANTIC_TARGETS_PRESERVED")
    if adjustments:
        out["comparison_candidate_adjustments"] = adjustments
    else:
        out.pop("comparison_candidate_adjustments", None)
    return out


def weighted_target_profile_rr(setup: Mapping[str, Any]) -> dict[str, Any]:
    """Compute signed per-target and normalized weighted R from W4 worst entry."""
    direction = setup.get("direction")
    zone = setup.get("entry_zone") or {}
    stop = _d(setup.get("stop_loss"))
    targets = [_d(t) for t in (setup.get("targets") or [])]
    if direction not in {"LONG", "SHORT"} or stop is None or not targets or any(t is None for t in targets):
        return {"valid": False, "reason": "missing direction/entry/SL/targets"}
    low, high = _d(zone.get("low")), _d(zone.get("high"))
    if low is None or high is None:
        return {"valid": False, "reason": "missing entry zone"}
    worst = high if direction == "LONG" else low
    risk = abs(worst - stop)
    if risk <= 0:
        return {"valid": False, "reason": "non-positive stop distance"}

    rrs: list[Decimal] = []
    for target in targets:
        reward = (target - worst) if direction == "LONG" else (worst - target)
        rrs.append(reward / risk)
    weights = list(_TARGET_WEIGHTS[:len(rrs)])
    if len(rrs) > len(weights):
        weights.extend([weights[-1]] * (len(rrs) - len(weights)))
    total_weight = sum(weights, Decimal("0"))
    weights = [w / total_weight for w in weights]
    weighted = sum((w * r for w, r in zip(weights, rrs)), Decimal("0"))
    return {
        "valid": all(r > 0 for r in rrs),
        "worst_case_entry": worst,
        "risk_distance": risk,
        "target_r": rrs,
        "weights": weights,
        "weighted_r": weighted,
        "reason": None if all(r > 0 for r in rrs) else "one or more targets are not on the profitable side of worst-case entry",
    }


def evaluate_risk_weighted_profile(
    symbol: str,
    cutoff: dt.datetime,
    regime: Mapping[str, Any],
    setup: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    orderbook: Mapping[str, Any],
    data_quality: Mapping[str, Any],
    portfolio: Mapping[str, Any],
) -> dict[str, Any]:
    """Use normal W4 unless only TP1 R:R blocks an otherwise valid profile."""
    base = rm.evaluate_risk(
        symbol, cutoff, dict(regime), dict(setup), dict(confirmation), dict(orderbook), dict(data_quality),
        dict(portfolio), rm.DEFAULT_RISK_CONFIG,
    )
    profile = weighted_target_profile_rr(setup)
    base["comparison_weighted_target_profile"] = profile
    reasons = list(base.get("rejection_reasons") or [])
    rr_only = bool(reasons) and all(str(r).startswith(_RR_REJECTION_PREFIX) for r in reasons)
    if base.get("decision") != "REJECTED" or not rr_only:
        return base

    threshold = Decimal(str(rm.DEFAULT_RISK_CONFIG["min_reward_to_risk"]))
    weighted = _d(profile.get("weighted_r")) if profile.get("valid") else None
    if weighted is None or weighted < threshold:
        return base

    cfg = copy.deepcopy(rm.DEFAULT_RISK_CONFIG)
    cfg["min_reward_to_risk"] = Decimal("0")
    out = rm.evaluate_risk(
        symbol, cutoff, dict(regime), dict(setup), dict(confirmation), dict(orderbook), dict(data_quality),
        dict(portfolio), cfg,
    )
    out["comparison_weighted_target_profile"] = profile
    if out.get("decision") in {"APPROVED", "APPROVED_REDUCED_SIZE", "WAIT_FOR_CONFIRMATION"}:
        out.setdefault("reasons", []).append(
            f"comparison semantic-target gate: weighted target profile {weighted:.3f}R clears {threshold}R; TP1-only rejection bypassed"
        )
        out["comparison_rr_gate"] = "WEIGHTED_TARGET_PROFILE"
    return out


def build_research_v2_semantic_candidate_chain_indexed(
    symbol: str,
    index: HistoricalWindowIndex,
    cutoff: dt.datetime,
    *,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
    portfolio_state: Optional[dict] = None,
) -> dict[str, Any]:
    w = index.windows(cutoff, include_execution_1m=False)
    built = rs.build_features_from_windows(
        w.candles_4h, w.candles_1h, w.candles_15m, w.trades_1m,
        w.liq_1m, w.liq_5m, w.liq_15m, w.orderbook, w.derivatives,
        now=cutoff, max_ages=RESEARCH_HIST_MAX_AGES,
    )
    w1_dq = classification_data_quality_for_degraded_replay(built["data_quality"])
    regime = rc.classify_from_features(
        symbol, cutoff, built["tf"], built["volume"], built["orderbook"], built["futures"],
        built["liquidations"], w1_dq, btc_regime=btc_regime, previous_state=previous_state,
    )
    state = regime.pop("_state", None)
    raw = sd.detect_setups(
        symbol, cutoff, regime, built["tf"], built["volume"], built["orderbook"],
        built["futures"], built["liquidations"],
    )
    setups = {k: repair_setup_geometry_preserve_targets(v, built) for k, v in raw["setups"].items()}
    confirmations: dict[str, dict] = {}; risk_decisions: dict[str, dict] = {}
    portfolio = portfolio_state or {
        "open_positions": [], "daily_realized_pnl_pct": Decimal("0"),
        "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False,
    }
    for setup_type, setup in setups.items():
        confirmation = ofc.confirm_setup(
            symbol, setup, regime, built["tf"], built["volume"], built["orderbook"],
            built["futures"], built["liquidations"], btc_regime=btc_regime,
            data_quality=built["data_quality"],
        )
        confirmations[setup_type] = confirmation
        risk_decisions[setup_type] = evaluate_risk_weighted_profile(
            symbol, cutoff, regime, setup, confirmation, built["orderbook"], built["data_quality"], portfolio,
        )
    return {
        "symbol": symbol, "as_of": cutoff, "variant": Variant.RESEARCH_V2,
        "candidate_kind": "RESEARCH_V2_SEMANTIC_TARGETS_WEIGHTED_RR_V1",
        "regime": regime, "setups": setups, "confirmations": confirmations,
        "risk_decisions": risk_decisions, "data_quality": built["data_quality"],
        "w1_classification_data_quality": w1_dq, "features": built,
        "_state": state, "_windows": w,
    }
