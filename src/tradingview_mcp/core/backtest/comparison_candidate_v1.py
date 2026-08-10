"""Research-only candidate repairs discovered by BACKTEST_COMPARISON_V1.

This module deliberately does not change the production Research V2 pipeline.
It builds a candidate W1->W4 chain on the same point-in-time inputs so the
repairs can be evaluated against the immutable baseline before promotion.

Repairs are contract/instrumentation driven, not outcome tuned:
1. A missing historical orderbook does not reduce confidence of price/volume/
   derivatives regimes when all classifier-critical inputs are healthy. The
   source stays unavailable and low-liquidity/orderbook regimes remain
   undetectable; only the generic confidence multiplier is de-biased.
2. Setup geometry must satisfy the W4 contract: SL must be outside the entry
   zone and targets are measured from W4's conservative worst-case entry.
3. Breakout invalidation is anchored around the broken level instead of the
   opposite edge of the whole prior range. A breakout that falls materially
   back through the broken level is invalid before a full-range traversal.

Nothing here writes DB/paper/exchange state.
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
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import RESEARCH_HIST_MAX_AGES
from tradingview_mcp.core.backtest.comparison_v1 import Variant
from tradingview_mcp.core.services import regime_service_v2 as rs


TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
BREAKOUT_INVALIDATION_ATR = Decimal("0.5")
MIN_ZONE_CLEARANCE_ATR = Decimal("0.1")


def _d(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def classification_data_quality_for_degraded_replay(data_quality: Mapping[str, Any]) -> dict[str, Any]:
    """De-bias W1 confidence when only historical orderbook is unavailable.

    The actual data-quality object is not changed for W3/W4. This copy is only
    passed into W1's confidence calculation. We never fabricate orderbook
    features: ``orderbook.available`` stays False, so liquidity regimes cannot
    be claimed from nonexistent history.
    """
    out = copy.deepcopy(dict(data_quality))
    missing = {str(x).replace("STALE:", "") for x in (out.get("missing_fields") or [])}
    stale = set(out.get("stale_fields") or [])
    if out.get("is_tradeable", False) and not stale and missing and missing <= {"orderbook"}:
        original = int(out.get("data_quality_score", 0) or 0)
        out["data_quality_score"] = 100
        out["comparison_confidence_score_original"] = original
        out["comparison_confidence_adjustment"] = "MISSING_HISTORICAL_ORDERBOOK_NOT_USED_AS_GENERIC_W1_CONFIDENCE_PENALTY"
    return out


def _outside_zone_stop(direction: str, zone_low: Decimal, zone_high: Decimal, stop: Decimal, atr: Decimal) -> Decimal:
    clearance = max(atr * MIN_ZONE_CLEARANCE_ATR, Decimal("0"))
    if direction == "LONG" and stop >= zone_low:
        return zone_low - clearance
    if direction == "SHORT" and stop <= zone_high:
        return zone_high + clearance
    return stop


def _w4_worst_entry(direction: str, zone_low: Decimal, zone_high: Decimal) -> Decimal:
    return zone_high if direction == "LONG" else zone_low


def _align_targets_to_w4_contract(setup: dict[str, Any]) -> None:
    direction = setup.get("direction")
    zone = setup.get("entry_zone") or {}
    stop = _d(setup.get("stop_loss"))
    targets = [_d(t) for t in (setup.get("targets") or [])]
    if direction not in {"LONG", "SHORT"} or stop is None or not targets:
        return
    zone_low, zone_high = _d(zone.get("low")), _d(zone.get("high"))
    if zone_low is None or zone_high is None:
        return
    worst = _w4_worst_entry(direction, zone_low, zone_high)
    risk = abs(worst - stop)
    if risk <= 0:
        return

    aligned = []
    for idx, target in enumerate(targets):
        mult = TARGET_R_MULTIPLIERS[min(idx, len(TARGET_R_MULTIPLIERS) - 1)]
        floor = worst + risk * mult if direction == "LONG" else worst - risk * mult
        if target is None:
            target = floor
        elif direction == "LONG":
            target = max(target, floor)
        else:
            target = min(target, floor)
        aligned.append(target)
    setup["targets"] = aligned


def repair_setup_geometry(setup: Mapping[str, Any], features: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy whose geometry is internally compatible with W4.

    No market outcome/future candle is consulted. Only features available at
    the decision timestamp are used.
    """
    out = copy.deepcopy(dict(setup))
    direction = out.get("direction")
    if direction not in {"LONG", "SHORT"}:
        return out

    zone = out.get("entry_zone") or {}
    zone_low, zone_high = _d(zone.get("low")), _d(zone.get("high"))
    pa_1h = ((features.get("tf") or {}).get("1h") or {})
    atr = _d(pa_1h.get("atr"))
    stop = _d(out.get("stop_loss"))
    if None in (zone_low, zone_high, atr, stop) or atr <= 0:
        return out

    adjustments: list[str] = []

    if out.get("setup_type") == "breakout":
        support = _d(pa_1h.get("support"))
        resistance = _d(pa_1h.get("resistance"))
        level = resistance if direction == "LONG" else support
        if level is not None:
            if direction == "LONG":
                candidate_stop = level - atr * BREAKOUT_INVALIDATION_ATR
                candidate_stop = min(candidate_stop, zone_low - atr * MIN_ZONE_CLEARANCE_ATR)
            else:
                candidate_stop = level + atr * BREAKOUT_INVALIDATION_ATR
                candidate_stop = max(candidate_stop, zone_high + atr * MIN_ZONE_CLEARANCE_ATR)
            if candidate_stop != stop:
                stop = candidate_stop
                out["invalidation"] = level
                adjustments.append("BREAKOUT_STOP_ANCHORED_TO_BROKEN_LEVEL")

    repaired_stop = _outside_zone_stop(direction, zone_low, zone_high, stop, atr)
    if repaired_stop != stop:
        stop = repaired_stop
        adjustments.append("SL_MOVED_OUTSIDE_ENTRY_ZONE")
    out["stop_loss"] = stop

    before_targets = list(out.get("targets") or [])
    _align_targets_to_w4_contract(out)
    if list(out.get("targets") or []) != before_targets:
        adjustments.append("TARGETS_ALIGNED_TO_W4_WORST_ENTRY_R_MULTIPLES")

    if adjustments:
        out["comparison_candidate_adjustments"] = adjustments
        out.setdefault("reasons", []).append("comparison candidate geometry repaired to satisfy the declared W2/W4 contract")
    return out


def build_research_v2_candidate_chain_indexed(
    symbol: str,
    index: HistoricalWindowIndex,
    cutoff: dt.datetime,
    *,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
    portfolio_state: Optional[dict] = None,
) -> dict[str, Any]:
    """Build a research candidate chain without mutating baseline modules."""
    w = index.windows(cutoff, include_execution_1m=False)
    built = rs.build_features_from_windows(
        w.candles_4h,
        w.candles_1h,
        w.candles_15m,
        w.trades_1m,
        w.liq_1m,
        w.liq_5m,
        w.liq_15m,
        w.orderbook,
        w.derivatives,
        now=cutoff,
        max_ages=RESEARCH_HIST_MAX_AGES,
    )
    w1_dq = classification_data_quality_for_degraded_replay(built["data_quality"])
    regime = rc.classify_from_features(
        symbol,
        cutoff,
        built["tf"],
        built["volume"],
        built["orderbook"],
        built["futures"],
        built["liquidations"],
        w1_dq,
        btc_regime=btc_regime,
        previous_state=previous_state,
    )
    state = regime.pop("_state", None)
    raw_setup_result = sd.detect_setups(
        symbol,
        cutoff,
        regime,
        built["tf"],
        built["volume"],
        built["orderbook"],
        built["futures"],
        built["liquidations"],
    )
    setups = {
        setup_type: repair_setup_geometry(setup, built)
        for setup_type, setup in raw_setup_result["setups"].items()
    }

    confirmations: dict[str, dict] = {}
    risk_decisions: dict[str, dict] = {}
    portfolio = portfolio_state or {
        "open_positions": [],
        "daily_realized_pnl_pct": Decimal("0"),
        "weekly_realized_pnl_pct": Decimal("0"),
        "btc_regime_flip_detected": False,
    }
    for setup_type, setup in setups.items():
        confirmation = ofc.confirm_setup(
            symbol,
            setup,
            regime,
            built["tf"],
            built["volume"],
            built["orderbook"],
            built["futures"],
            built["liquidations"],
            btc_regime=btc_regime,
            data_quality=built["data_quality"],
        )
        confirmations[setup_type] = confirmation
        risk_decisions[setup_type] = rm.evaluate_risk(
            symbol,
            cutoff,
            regime,
            setup,
            confirmation,
            built["orderbook"],
            built["data_quality"],
            portfolio,
            rm.DEFAULT_RISK_CONFIG,
        )

    return {
        "symbol": symbol,
        "as_of": cutoff,
        "variant": Variant.RESEARCH_V2,
        "candidate_kind": "RESEARCH_V2_CONTRACT_REPAIRED_V1",
        "regime": regime,
        "setups": setups,
        "confirmations": confirmations,
        "risk_decisions": risk_decisions,
        "data_quality": built["data_quality"],
        "w1_classification_data_quality": w1_dq,
        "features": built,
        "_state": state,
        "_windows": w,
    }
