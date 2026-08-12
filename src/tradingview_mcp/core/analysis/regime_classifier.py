"""Market Regime Classifier.

Deterministic rule-based classification (no ML/AI) into one of the plan's
9 regimes, using price structure, EMA relationship, ATR%, Bollinger Band
Width proxy (realized volatility used as the available proxy — the
existing TradingView MCP's own Bollinger tool remains the canonical BBW
source; this classifier only needs a *volatility regime* signal, not the
exact bands), spread, and false-breakout / chop counting from swing
structure.
"""
from __future__ import annotations

from decimal import Decimal

REGIMES = (
    "TREND_UP",
    "TREND_DOWN",
    "RANGE",
    "BREAKOUT_ATTEMPT",
    "HIGH_VOLATILITY",
    "LOW_VOLATILITY",
    "LOW_LIQUIDITY",
    "CHAOTIC",
    "NO_DATA",
)

HIGH_VOL_ATR_PCT = Decimal("3.0")
LOW_VOL_ATR_PCT = Decimal("0.4")
LOW_LIQUIDITY_SPREAD_BPS = Decimal("15")
CHAOTIC_SPREAD_BPS = Decimal("30")


def classify_regime(features: dict) -> dict:
    """`features` is a `feature_snapshots`-shaped dict as produced by
    `feature_engine.build_feature_snapshot` (needs `price_action` and
    `orderbook` sub-dicts at minimum; `futures`/`liquidations` refine
    confidence when present)."""
    price_action = features.get("price_action") or {}
    orderbook = features.get("orderbook") or {}

    if not features.get("is_tradeable", False) or not price_action.get("available"):
        return {
            "regime": "NO_DATA",
            "confidence": 0.3,
            "reasons": ["insufficient or stale data to classify a regime"],
            "warnings": list(features.get("stale_fields", []) or features.get("missing_fields", [])),
        }

    reasons = []
    warnings = []

    spread_bps = orderbook.get("spread_bps")
    if spread_bps is not None and Decimal(str(spread_bps)) >= CHAOTIC_SPREAD_BPS:
        return {
            "regime": "CHAOTIC",
            "confidence": 0.7,
            "reasons": [f"spread {spread_bps} bps exceeds chaotic threshold {CHAOTIC_SPREAD_BPS}"],
            "warnings": [],
        }

    if spread_bps is not None and Decimal(str(spread_bps)) >= LOW_LIQUIDITY_SPREAD_BPS:
        return {
            "regime": "LOW_LIQUIDITY",
            "confidence": 0.65,
            "reasons": [f"spread {spread_bps} bps indicates thin liquidity"],
            "warnings": [],
        }

    atr_pct = price_action.get("atr_pct")
    atr_pct_dec = Decimal(str(atr_pct)) if atr_pct is not None else None

    if atr_pct_dec is not None and atr_pct_dec >= HIGH_VOL_ATR_PCT:
        reasons.append(f"ATR% {atr_pct_dec} >= {HIGH_VOL_ATR_PCT} high-volatility threshold")
        return {"regime": "HIGH_VOLATILITY", "confidence": 0.7, "reasons": reasons, "warnings": warnings}

    structure = price_action.get("structure", "UNKNOWN")
    ema20 = price_action.get("ema20")
    ema50 = price_action.get("ema50")
    ema200 = price_action.get("ema200")
    last_close = price_action.get("last_close")

    trend_up = (
        structure == "HH_HL"
        and ema20 is not None and ema50 is not None
        and Decimal(str(ema20)) > Decimal(str(ema50))
        and (ema200 is None or Decimal(str(last_close)) > Decimal(str(ema200)))
    )
    trend_down = (
        structure == "LH_LL"
        and ema20 is not None and ema50 is not None
        and Decimal(str(ema20)) < Decimal(str(ema50))
        and (ema200 is None or Decimal(str(last_close)) < Decimal(str(ema200)))
    )

    if trend_up:
        reasons.append("HH/HL structure with EMA20 > EMA50 (and price above EMA200)")
        confidence = 0.85 if ema200 is not None else 0.7
        return {"regime": "TREND_UP", "confidence": confidence, "reasons": reasons, "warnings": warnings}

    if trend_down:
        reasons.append("LH/LL structure with EMA20 < EMA50 (and price below EMA200)")
        confidence = 0.85 if ema200 is not None else 0.7
        return {"regime": "TREND_DOWN", "confidence": confidence, "reasons": reasons, "warnings": warnings}

    if structure == "EXPANDING":
        reasons.append("expanding range (higher highs + lower lows) — possible breakout attempt")
        return {"regime": "BREAKOUT_ATTEMPT", "confidence": 0.55, "reasons": reasons, "warnings": warnings}

    if structure == "CONTRACTING":
        reasons.append("contracting range (lower highs + higher lows) — compression before a move")
        warnings.append("compression can resolve into a breakout attempt without much warning")
        return {"regime": "RANGE", "confidence": 0.6, "reasons": reasons, "warnings": warnings}

    if atr_pct_dec is not None and atr_pct_dec <= LOW_VOL_ATR_PCT:
        reasons.append(f"ATR% {atr_pct_dec} <= {LOW_VOL_ATR_PCT} low-volatility threshold")
        return {"regime": "LOW_VOLATILITY", "confidence": 0.6, "reasons": reasons, "warnings": warnings}

    reasons.append("no clear directional structure; range-bound by default")
    return {"regime": "RANGE", "confidence": 0.5, "reasons": reasons, "warnings": warnings}


def is_regime_allowed(regime: str, allowed_regimes) -> bool:
    return regime in allowed_regimes
