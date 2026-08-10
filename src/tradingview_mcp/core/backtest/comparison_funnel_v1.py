"""Research-only funnel diagnostics for BACKTEST_COMPARISON_V1.

Counts where Research V2 opportunities disappear across W1 -> W2 -> W3 -> W4.
The module is observational only: it never changes a setup, confirmation, risk
policy, paper state, database row, or exchange order.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Mapping


ACTIVE_DIRECTIONS = {"LONG", "SHORT"}
ORDERBOOK_DIMENSIONS = {"orderbook_imbalance", "spread_and_depth", "microprice"}
LIQUIDATION_DIMENSIONS = {"liquidations_longs_shorts"}


def _reason_bucket(reason: str) -> str:
    text = str(reason or "").lower()
    if "does not support" in text:
        return "REGIME_INCOMPATIBLE"
    if "has not pulled back" in text:
        return "PULLBACK_NOT_IN_EMA_ZONE"
    if "extended, no pullback" in text:
        return "PULLBACK_TOO_EXTENDED"
    if "mid-range" in text or "not at an extreme" in text:
        return "MEAN_REVERSION_NOT_AT_EXTREME"
    if "squeeze detected" in text and "direction" in text:
        return "SQUEEZE_DIRECTION_UNRESOLVED"
    if "calibrated confidence" in text and "below" in text:
        return "SETUP_CONFIDENCE_BELOW_FLOOR"
    if "insufficient" in text or "unavailable" in text:
        return "INSUFFICIENT_SETUP_INPUTS"
    if "data quality" in text or "stale/missing critical" in text:
        return "DATA_QUALITY_GATE"
    if "reward" in text and "risk" in text:
        return "REWARD_RISK_GATE"
    if "spread" in text:
        return "SPREAD_GATE"
    if "portfolio limit" in text or "exposure" in text:
        return "PORTFOLIO_LIMIT"
    if "max_open_positions" in text or "position" in text and "already" in text:
        return "POSITION_LIMIT"
    if "daily loss" in text or "weekly loss" in text:
        return "LOSS_LIMIT"
    return str(reason or "UNKNOWN")[:160]


def _counter_dict(counter: Counter) -> dict[str, int]:
    return {str(k): int(v) for k, v in counter.most_common()}


class ResearchFunnelDiagnostics:
    """Accumulate deterministic diagnostics from already-built W1-W4 chains."""

    def __init__(self) -> None:
        self.decision_points = 0
        self.regimes = Counter()
        self.regimes_by_symbol: dict[str, Counter] = defaultdict(Counter)
        self.data_quality_scores = Counter()
        self.tradeable = Counter()
        self.missing_feature_sources = Counter()
        self.setup_candidates = Counter()
        self.setup_candidates_by_symbol: dict[str, Counter] = defaultdict(Counter)
        self.setup_no_setup = Counter()
        self.setup_no_setup_reasons = Counter()
        self.confirmations = Counter()
        self.confirmations_by_setup: dict[str, Counter] = defaultdict(Counter)
        self.confirmation_available_dimensions = Counter()
        self.confirmation_net_scores = Counter()
        self.confirmation_missing_dimensions = Counter()
        self.confirmation_reads = Counter()
        self.confirmation_already_used_buckets = Counter()
        self.risk_decisions = Counter()
        self.risk_decisions_by_setup: dict[str, Counter] = defaultdict(Counter)
        self.risk_reason_buckets = Counter()
        self.approved_by_confirmation = Counter()
        self.candidate_by_regime = Counter()

    def record(self, chain: Mapping[str, Any]) -> None:
        symbol = str(chain.get("symbol") or "UNKNOWN")
        self.decision_points += 1

        regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
        self.regimes[regime] += 1
        self.regimes_by_symbol[symbol][regime] += 1

        dq = chain.get("data_quality") or {}
        self.data_quality_scores[str(dq.get("data_quality_score", "UNKNOWN"))] += 1
        self.tradeable[str(bool(dq.get("is_tradeable", False)))] += 1
        for src in dq.get("missing_fields", []) or []:
            self.missing_feature_sources[str(src)] += 1
        for src in dq.get("stale_fields", []) or []:
            self.missing_feature_sources[f"STALE:{src}"] += 1

        setups = chain.get("setups") or {}
        confirmations = chain.get("confirmations") or {}
        risks = chain.get("risk_decisions") or {}
        for setup_type, setup in setups.items():
            direction = setup.get("direction")
            if direction not in ACTIVE_DIRECTIONS:
                self.setup_no_setup[str(setup_type)] += 1
                reasons = setup.get("reasons") or ["UNKNOWN"]
                self.setup_no_setup_reasons[f"{setup_type}:{_reason_bucket(reasons[0])}"] += 1
                continue

            self.setup_candidates[str(setup_type)] += 1
            self.setup_candidates_by_symbol[symbol][str(setup_type)] += 1
            self.candidate_by_regime[regime] += 1

            confirmation = confirmations.get(setup_type) or {}
            status = str(confirmation.get("status") or "UNKNOWN")
            self.confirmations[status] += 1
            self.confirmations_by_setup[str(setup_type)][status] += 1
            self.confirmation_available_dimensions[str(confirmation.get("available_dimensions", "UNKNOWN"))] += 1
            self.confirmation_net_scores[str(confirmation.get("net_independent_score", "UNKNOWN"))] += 1
            for dim in confirmation.get("missing_sources", []) or []:
                self.confirmation_missing_dimensions[str(dim)] += 1
            for bucket in set(confirmation.get("used_by_regime", []) or []) | set(confirmation.get("used_by_setup", []) or []):
                self.confirmation_already_used_buckets[str(bucket)] += 1
            for dim in confirmation.get("dimensions", []) or []:
                self.confirmation_reads[f"{dim.get('dimension')}:{dim.get('read')}"] += 1

            risk = risks.get(setup_type) or {}
            decision = str(risk.get("decision") or "UNKNOWN")
            self.risk_decisions[decision] += 1
            self.risk_decisions_by_setup[str(setup_type)][decision] += 1
            if decision in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
                self.approved_by_confirmation[status] += 1
            else:
                reasons = (risk.get("rejection_reasons") or []) + (risk.get("reasons") or [])
                if not reasons:
                    reasons = ["NO_EXPLICIT_REASON"]
                for reason in reasons:
                    self.risk_reason_buckets[_reason_bucket(str(reason))] += 1

    def to_dict(self) -> dict[str, Any]:
        candidates = sum(self.setup_candidates.values())
        approved = sum(v for k, v in self.risk_decisions.items() if k in {"APPROVED", "APPROVED_REDUCED_SIZE"})
        missing_ob = sum(self.confirmation_missing_dimensions[d] for d in ORDERBOOK_DIMENSIONS)
        missing_liq = sum(self.confirmation_missing_dimensions[d] for d in LIQUIDATION_DIMENSIONS)
        active_dimensions_total = sum(self.confirmation_available_dimensions.values())
        return {
            "decision_points_total": self.decision_points,
            "w1_regimes": _counter_dict(self.regimes),
            "w1_regimes_by_symbol": {s: _counter_dict(c) for s, c in sorted(self.regimes_by_symbol.items())},
            "data_quality_scores": _counter_dict(self.data_quality_scores),
            "data_quality_tradeable": _counter_dict(self.tradeable),
            "data_quality_missing_or_stale": _counter_dict(self.missing_feature_sources),
            "w2_active_candidates": _counter_dict(self.setup_candidates),
            "w2_active_candidates_by_symbol": {s: _counter_dict(c) for s, c in sorted(self.setup_candidates_by_symbol.items())},
            "w2_no_setup_counts": _counter_dict(self.setup_no_setup),
            "w2_top_no_setup_reasons": _counter_dict(self.setup_no_setup_reasons),
            "w3_statuses": _counter_dict(self.confirmations),
            "w3_statuses_by_setup": {s: _counter_dict(c) for s, c in sorted(self.confirmations_by_setup.items())},
            "w3_available_dimensions_distribution": _counter_dict(self.confirmation_available_dimensions),
            "w3_net_independent_score_distribution": _counter_dict(self.confirmation_net_scores),
            "w3_missing_dimensions": _counter_dict(self.confirmation_missing_dimensions),
            "w3_dimension_reads": _counter_dict(self.confirmation_reads),
            "w3_upstream_used_bucket_counts": _counter_dict(self.confirmation_already_used_buckets),
            "w4_decisions": _counter_dict(self.risk_decisions),
            "w4_decisions_by_setup": {s: _counter_dict(c) for s, c in sorted(self.risk_decisions_by_setup.items())},
            "w4_top_wait_reject_reasons": _counter_dict(self.risk_reason_buckets),
            "w4_approved_by_w3_status": _counter_dict(self.approved_by_confirmation),
            "derived_rates": {
                "w2_candidates_per_decision_point": (candidates / self.decision_points) if self.decision_points else 0.0,
                "w4_approved_per_w2_candidate": (approved / candidates) if candidates else 0.0,
                "final_approved_per_decision_point": (approved / self.decision_points) if self.decision_points else 0.0,
            },
            "degraded_w3_impact": {
                "active_setup_confirmations_observed": active_dimensions_total,
                "missing_orderbook_dimension_events": int(missing_ob),
                "missing_liquidation_dimension_events": int(missing_liq),
                "note": (
                    "Each active setup can lose up to 3 orderbook dimensions and 1 liquidation dimension in the extended degraded replay. "
                    "Counts are diagnostic, not a counterfactual claim that those missing dimensions would have confirmed the setup."
                ),
            },
        }
