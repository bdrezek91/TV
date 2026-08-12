"""Research-only V13 breakout candidate.

V13 composes the existing point-in-time volatility-expansion detector with
pre-outcome gates for directional regime, persistent order flow, open-interest
expansion and funding crowding.  It never reads future candles or realized PnL.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

from tradingview_mcp.core.backtest.comparison_volatility_expansion_v1 import (
    evaluate_volatility_expansion,
)

V13_NAME = "V13_REGIME_BREAKOUT_PERSISTENT_FLOW_OI"
RESERVED_HOLDOUT_MARKER = "holdout_mrv2_120d"
CVD_FLAT_BAND = Decimal("1")
TAKER_IMBALANCE_MIN = Decimal("0.05")
FUNDING_EXTREME_ABS = Decimal("0.0005")
ALLOWED_REGIMES = {
    "LONG": {"TREND_UP", "BREAKOUT_UP"},
    "SHORT": {"TREND_DOWN", "BREAKOUT_DOWN"},
}
V13_BASE_BREAKOUT_REGIMES = {"TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN"}
OI_RELATION = {
    "LONG": "PRICE_UP_OI_UP",
    "SHORT": "PRICE_DOWN_OI_UP",
}


def validate_v13_development_cache_root(cache_root: Path) -> Path:
    """Fail closed when development points at the reserved outcome-unseen cache."""
    resolved = cache_root.resolve()
    if RESERVED_HOLDOUT_MARKER in str(resolved).lower():
        raise ValueError("V13 development runner refuses the reserved 120d holdout cache")
    return resolved


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _direction_sign(direction: str) -> Decimal:
    return Decimal("1") if direction == "LONG" else Decimal("-1")


def evaluate_v13_gates(chain: Mapping[str, Any], direction: str) -> dict[str, Any]:
    """Evaluate the frozen V13 eligibility gates at the decision timestamp."""
    reasons: list[str] = []
    rejection_reasons: list[str] = []
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    features = chain.get("features") or {}
    volume = features.get("volume") or {}
    futures = features.get("futures") or {}

    if direction not in {"LONG", "SHORT"}:
        rejection_reasons.append("base breakout did not produce LONG/SHORT")
        return {
            "candidate": V13_NAME,
            "eligible": False,
            "direction": direction,
            "reasons": reasons,
            "rejection_reasons": rejection_reasons,
            "future_data_used": False,
        }

    if regime not in ALLOWED_REGIMES[direction]:
        rejection_reasons.append(f"regime {regime} is not directionally compatible with {direction}")
    else:
        reasons.append(f"regime {regime} supports {direction}")

    if not bool(volume.get("available")):
        rejection_reasons.append("volume/CVD features unavailable")
        cvd_slope = buy = sell = taker_imbalance = None
    else:
        cvd_slope = _d(volume.get("cvd_slope_15m"))
        buy = _d(volume.get("buy_taker_volume"))
        sell = _d(volume.get("sell_taker_volume"))
        taker_imbalance = None if buy is None or sell is None or buy + sell <= 0 else (buy - sell) / (buy + sell)
        sign = _direction_sign(direction)
        if cvd_slope is None or cvd_slope * sign < CVD_FLAT_BAND:
            rejection_reasons.append("15m CVD slope is not persistently aligned with breakout direction")
        else:
            reasons.append(f"15m CVD slope {cvd_slope} aligns with {direction}")
        if taker_imbalance is None or taker_imbalance * sign < TAKER_IMBALANCE_MIN:
            rejection_reasons.append("taker buy/sell imbalance is not aligned strongly enough")
        else:
            reasons.append(f"taker imbalance {taker_imbalance} aligns with {direction}")

    if not bool(futures.get("available")):
        rejection_reasons.append("derivatives features unavailable")
        oi_relation = funding = None
    else:
        oi_relation = futures.get("price_to_oi_relation")
        funding = _d(futures.get("funding_rate"))
        if oi_relation != OI_RELATION[direction]:
            rejection_reasons.append(
                f"price/OI relation {oi_relation} does not show fresh exposure supporting {direction}"
            )
        else:
            reasons.append(f"price/OI relation {oi_relation} confirms fresh directional exposure")
        if funding is None:
            rejection_reasons.append("funding rate unavailable")
        else:
            crowded_with_direction = (
                (direction == "LONG" and funding >= FUNDING_EXTREME_ABS)
                or (direction == "SHORT" and funding <= -FUNDING_EXTREME_ABS)
            )
            if crowded_with_direction:
                rejection_reasons.append(f"funding {funding} is extremely crowded with {direction}")
            else:
                reasons.append(f"funding {funding} is not extremely crowded with {direction}")

    return {
        "candidate": V13_NAME,
        "eligible": not rejection_reasons,
        "direction": direction,
        "regime": regime,
        "cvd_slope_15m": cvd_slope,
        "taker_imbalance": taker_imbalance,
        "price_to_oi_relation": oi_relation,
        "funding_rate": funding,
        "reasons": reasons,
        "rejection_reasons": rejection_reasons,
        "future_data_used": False,
    }


def evaluate_breakout_v13(chain: Mapping[str, Any], *, btc_regime: str | None = None) -> dict[str, Any]:
    """Return the existing breakout evaluation plus fail-closed V13 gates."""
    base = evaluate_volatility_expansion(
        chain,
        btc_regime=btc_regime,
        allowed_regimes=V13_BASE_BREAKOUT_REGIMES,
    )
    setup = base["setup"]
    direction = str(setup.get("direction") or "NO_SETUP")
    gates = evaluate_v13_gates(chain, direction)
    signal = base.get("signal") if gates["eligible"] else None
    return {
        "candidate": V13_NAME,
        "setup": setup,
        "confirmation": base.get("confirmation"),
        "risk": base.get("risk"),
        "gates": gates,
        "signal": signal,
    }
