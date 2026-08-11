"""Post-holdout Mean Reversion V2 research hypothesis.

This module is deliberately research-only. MR V1 failed its untouched 90-day
holdout. The failed period was then opened for post-mortem analysis and may no
longer be used as validation data for this hypothesis.

MR_V2_EXHAUSTION_RANGE_QUALITY is the single structural hypothesis selected
from that post-mortem. It does NOT filter by symbol, clock hour, direction or
W3 label. It asks whether a RANGE extreme looks like an exhaustion/unwind that
can plausibly revert rather than a low-energy contraction or a higher-timeframe
expansion that is more likely to continue.

Frozen V2 gates at the decision timestamp:
1. the existing MR V1 setup must already be W4-approved;
2. 15m structure must NOT be CONTRACTING;
3. 4h structure must NOT be EXPANDING;
4. futures features must be available;
5. 1h open-interest change must be <= 0%.

The OI rule is intentionally direction-agnostic: falling/non-expanding OI is
interpreted as position unwinding/exhaustion rather than fresh leveraged risk
being added to the move. No future candle or realized outcome is consulted.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

MR_V2_NAME = "MR_V2_EXHAUSTION_RANGE_QUALITY"
MR_V2_SETUP = "mean_reversion"
MR_V2_REJECT_15M_STRUCTURE = "CONTRACTING"
MR_V2_REJECT_4H_STRUCTURE = "EXPANDING"
MR_V2_MAX_OI_CHANGE_1H_PCT = Decimal("0")


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def evaluate_mean_reversion_v2(chain: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the frozen V2 eligibility gate from point-in-time chain data."""
    setup = (chain.get("setups") or {}).get(MR_V2_SETUP) or {}
    risk = (chain.get("risk_decisions") or {}).get(MR_V2_SETUP) or {}
    features = chain.get("features") or {}
    tf = features.get("tf") or {}
    pa_15m = tf.get("15m") or {}
    pa_4h = tf.get("4h") or {}
    futures = features.get("futures") or {}

    reasons: list[str] = []
    rejection_reasons: list[str] = []

    direction = str(setup.get("direction") or "NO_SETUP")
    if direction not in {"LONG", "SHORT"}:
        rejection_reasons.append("existing MR V1 did not produce an actionable LONG/SHORT setup")

    risk_decision = str(risk.get("decision") or "UNKNOWN")
    if risk_decision not in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
        rejection_reasons.append(f"existing W4 decision is {risk_decision}, not approved")

    structure_15m = str(pa_15m.get("structure") or "UNKNOWN")
    if structure_15m == MR_V2_REJECT_15M_STRUCTURE:
        rejection_reasons.append("15m structure is CONTRACTING: insufficient active rotation for V2")
    else:
        reasons.append(f"15m structure {structure_15m} is not CONTRACTING")

    structure_4h = str(pa_4h.get("structure") or "UNKNOWN")
    if structure_4h == MR_V2_REJECT_4H_STRUCTURE:
        rejection_reasons.append("4h structure is EXPANDING: range-reversion thesis is vulnerable to continuation")
    else:
        reasons.append(f"4h structure {structure_4h} is not EXPANDING")

    if not bool(futures.get("available")):
        rejection_reasons.append("futures features unavailable: OI exhaustion gate fails closed")
        oi_change_1h = None
    else:
        oi_change_1h = _d(futures.get("oi_change_1h_pct"))
        if oi_change_1h is None:
            rejection_reasons.append("1h OI change unavailable: exhaustion gate fails closed")
        elif oi_change_1h > MR_V2_MAX_OI_CHANGE_1H_PCT:
            rejection_reasons.append(
                f"1h OI change {oi_change_1h}% > 0%: fresh leveraged exposure is still expanding"
            )
        else:
            reasons.append(f"1h OI change {oi_change_1h}% <= 0% supports unwind/exhaustion")

    return {
        "candidate": MR_V2_NAME,
        "eligible": not rejection_reasons,
        "direction": direction,
        "risk_decision": risk_decision,
        "structure_15m": structure_15m,
        "structure_4h": structure_4h,
        "oi_change_1h_pct": oi_change_1h,
        "reasons": reasons,
        "rejection_reasons": rejection_reasons,
        "future_data_used": False,
    }


def estimated_full_fill_round_trip_cost_r(
    *,
    direction: str,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    taker_fee_rate_pct: Any = Decimal("0.055"),
    slippage_bps_each_side: Any = Decimal("2"),
) -> Optional[Decimal]:
    """Conservative decision-time cost diagnostic, not a V2 eligibility gate.

    Assumes a full fill and one entry + one exit, both taker, with the same
    slippage on each side. Funding and partial-fill path effects are excluded.
    Quantity cancels because the estimate is expressed as a fraction of the
    fixed-risk amount.
    """
    low, high, stop = _d(entry_low), _d(entry_high), _d(stop_loss)
    fee_pct = _d(taker_fee_rate_pct)
    slip_bps = _d(slippage_bps_each_side)
    if None in (low, high, stop, fee_pct, slip_bps):
        return None
    if direction not in {"LONG", "SHORT"}:
        return None
    worst_entry = high if direction == "LONG" else low
    risk_distance = abs(worst_entry - stop)
    if risk_distance <= 0 or worst_entry <= 0:
        return None
    round_trip_fraction = Decimal("2") * (
        fee_pct / Decimal("100") + slip_bps / Decimal("10000")
    )
    return round_trip_fraction * worst_entry / risk_distance
