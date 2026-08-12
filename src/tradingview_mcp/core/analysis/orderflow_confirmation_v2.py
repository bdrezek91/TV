"""Order Flow Confirmation -- Warstwa 3 (research layer).

Takes an EXISTING setup from Warstwa 2 (`setup_detector_v2`) and asks one
question only: does current order flow confirm, partially confirm, stay
neutral on, contradict, or lack enough data to judge that setup? This
module NEVER generates a setup of its own and NEVER changes a setup's
direction -- it is strictly a confirmation layer bolted onto an already-
decided LONG/SHORT call.

Five possible verdicts: CONFIRMED, WEAK_CONFIRMATION, NEUTRAL,
CONTRADICTED, INSUFFICIENT_DATA.

NO DOUBLE COUNTING (the key rule): Warstwa 1 (regime) and Warstwa 2
(setup) already read some of this same order-flow data -- e.g. a
TREND_UP call already used CVD/OI/funding as confirming/contradicting
reasons. Re-reading the identical observation here and letting it push
Warstwa 3's confidence up AGAIN would be circular, not new evidence. Every
dimension this module evaluates is tagged with which upstream layer(s)
already cited it (`_used_buckets` scans `regime_full`/`setup`'s own
`reasons`/`counterarguments` text for the underlying feature category) --
only dimensions NEITHER layer already used count toward
`new_independent_confirmations` and Warstwa 3's own confidence.
Contradictions always count (fresh risk is always worth surfacing, even
if that feature category happens to have been "used" elsewhere), but a
CONFIRMS reading never counts twice.

Missing data is never treated as neutral confirmation: an unavailable
dimension is tagged UNAVAILABLE, excluded from the confirm/contradict
tally entirely, and surfaced in `missing_sources`. Too few readable
dimensions (< MIN_AVAILABLE_DIMENSIONS_FOR_VERDICT) forces
INSUFFICIENT_DATA regardless of what the few available ones say. This
layer NEVER blocks a price-only setup for lacking order flow -- it just
reports the verdict honestly; whether to act on a price-only setup
anyway is a decision for a later layer (Warstwa 4), not this one.

Pure functions only: no DB/network access.

All thresholds marked "HYPOTHESIS" are reasonable starting points, not
tuned against this project's own data yet.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

CONFIRMATION_STATUSES = ("CONFIRMED", "WEAK_CONFIRMATION", "NEUTRAL", "CONTRADICTED", "INSUFFICIENT_DATA")

# --- HYPOTHESIS thresholds: reasonable starting points, not yet tuned ---
MIN_AVAILABLE_DIMENSIONS_FOR_VERDICT = 3
FUNDING_EXTREME_ABS = Decimal("0.0005")
LONG_SHORT_SKEW_NEUTRAL_BAND = Decimal("0.15")
LIQUIDATION_IMBALANCE_STRONG = Decimal("0.2")
ORDERBOOK_IMBALANCE_STRONG = Decimal("0.2")
SPREAD_BPS_POOR_EXECUTION = Decimal("15")
MICROPRICE_SKEW_NEUTRAL_BAND = Decimal("0.0002")
LARGE_TRADE_MIN_COUNT = 3
TAKER_RATIO_NEUTRAL_BAND = Decimal("0.05")
CVD_FLAT_BAND = Decimal("1")

# Which feature-category "bucket" each dimension belongs to, for matching
# against upstream reasons/counterarguments text.
_BUCKET_KEYWORDS = {
    "cvd": ("cvd",),
    "volume": ("volume", "taker"),
    "oi": ("open interest", "oi%", " oi "),
    "funding": ("funding",),
    "positioning": ("long/short", "long_short", "positioning"),
    "liquidation": ("liquidat",),
    "orderbook": ("spread", "liquidity", "order-book", "orderbook", "microprice", "imbalance"),
    "btc": ("btc",),
}


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _used_buckets(texts: List[str]) -> set:
    joined = " ".join(texts).lower()
    return {bucket for bucket, kws in _BUCKET_KEYWORDS.items() if any(kw in joined for kw in kws)}


def _sign(direction: str) -> int:
    return 1 if direction == "LONG" else -1


def _dim(dimension: str, bucket: str, read: str, detail: str) -> dict:
    return {"dimension": dimension, "bucket": bucket, "read": read, "detail": detail}


def _unavailable(dimension: str, bucket: str, reason: str) -> dict:
    return _dim(dimension, bucket, "UNAVAILABLE", reason)


# ── Individual order-flow dimensions ────────────────────────────────────

def _dim_cvd(volume: dict, direction: str) -> dict:
    name, bucket = "cvd_direction_slope_divergence", "cvd"
    if not volume.get("available"):
        return _unavailable(name, bucket, "volume/CVD data unavailable")
    slope = _d(volume.get("cvd_slope_15m"))
    if slope is None:
        return _unavailable(name, bucket, "cvd_slope_15m missing")
    if abs(slope) < CVD_FLAT_BAND:
        return _dim(name, bucket, "NEUTRAL", f"CVD slope {slope} effectively flat")
    aligned = (slope > 0 and _sign(direction) > 0) or (slope < 0 and _sign(direction) < 0)
    if aligned:
        return _dim(name, bucket, "CONFIRMS", f"CVD slope {slope} aligned with {direction}")
    return _dim(name, bucket, "CONTRADICTS", f"CVD slope {slope} diverges from {direction} -- possible divergence")


def _dim_taker_ratio(volume: dict, direction: str) -> dict:
    name, bucket = "taker_buy_sell_ratio", "volume"
    if not volume.get("available"):
        return _unavailable(name, bucket, "volume data unavailable")
    buy, sell = _d(volume.get("buy_taker_volume")), _d(volume.get("sell_taker_volume"))
    if buy is None or sell is None or (buy + sell) == 0:
        return _unavailable(name, bucket, "taker buy/sell volume missing")
    ratio = (buy - sell) / (buy + sell)
    if abs(ratio) < TAKER_RATIO_NEUTRAL_BAND:
        return _dim(name, bucket, "NEUTRAL", f"taker buy/sell ratio {ratio} roughly balanced")
    aligned = (ratio > 0 and _sign(direction) > 0) or (ratio < 0 and _sign(direction) < 0)
    return _dim(name, bucket, "CONFIRMS" if aligned else "CONTRADICTS", f"taker buy/sell ratio {ratio}")


def _dim_oi(futures: dict, direction: str) -> dict:
    """Folds OI-change and price-to-OI relation into one dimension --
    feature_engine already couples them (`price_to_oi_relation` IS the
    price/OI-change comparison), so treating them separately would just
    be the same underlying observation counted twice within Warstwa 3
    itself."""
    name, bucket = "open_interest_and_price_to_oi", "oi"
    if not futures.get("available"):
        return _unavailable(name, bucket, "derivatives data unavailable")
    rel = futures.get("price_to_oi_relation")
    if rel is None:
        return _unavailable(name, bucket, "price_to_oi_relation unavailable (insufficient OI/price history)")
    sign = _sign(direction)
    # Semantics per feature_engine.compute_futures_features:
    #   PRICE_UP_OI_UP    -- new longs entering, trend-confirming
    #   PRICE_DOWN_OI_UP  -- new shorts entering, trend-confirming
    #   PRICE_UP_OI_DOWN  -- short covering, weaker confirmation (not a clean confirm)
    #   PRICE_DOWN_OI_DOWN-- long liquidation/close, weaker confirmation
    if rel == "PRICE_UP_OI_UP":
        return _dim(name, bucket, "CONFIRMS" if sign > 0 else "CONTRADICTS", "new longs entering alongside rising price")
    if rel == "PRICE_DOWN_OI_UP":
        return _dim(name, bucket, "CONFIRMS" if sign < 0 else "CONTRADICTS", "new shorts entering alongside falling price")
    if rel == "PRICE_UP_OI_DOWN":
        return _dim(name, bucket, "NEUTRAL" if sign > 0 else "CONTRADICTS", "price up on falling OI -- short covering, not new demand")
    if rel == "PRICE_DOWN_OI_DOWN":
        return _dim(name, bucket, "NEUTRAL" if sign < 0 else "CONTRADICTS", "price down on falling OI -- long liquidation/close, not new supply")
    return _dim(name, bucket, "NEUTRAL", f"price/OI relation flat ({rel})")


def _dim_funding(futures: dict, direction: str) -> dict:
    """Funding is context only, NEVER a standalone confirming signal --
    same principle Warstwa 1 already applies. Extreme funding can only
    ever CONTRADICT (crowding/squeeze risk against the position) or stay
    NEUTRAL, never independently CONFIRM."""
    name, bucket = "funding_extremity", "funding"
    if not futures.get("available"):
        return _unavailable(name, bucket, "derivatives data unavailable")
    funding = _d(futures.get("funding_rate"))
    if funding is None:
        return _unavailable(name, bucket, "funding_rate missing")
    if abs(funding) < FUNDING_EXTREME_ABS:
        return _dim(name, bucket, "NEUTRAL", f"funding {funding} not extreme")
    crowded_with_us = (funding > 0 and _sign(direction) > 0) or (funding < 0 and _sign(direction) < 0)
    if crowded_with_us:
        return _dim(name, bucket, "CONTRADICTS", f"extreme funding {funding} crowded in our own direction -- squeeze risk against the position")
    return _dim(name, bucket, "NEUTRAL", f"extreme funding {funding} crowded against our direction -- noted, not treated as standalone confirmation")


def _dim_long_short_ratio(futures: dict, direction: str) -> dict:
    name, bucket = "long_short_ratio", "positioning"
    if not futures.get("available"):
        return _unavailable(name, bucket, "derivatives data unavailable")
    ratio = _d(futures.get("long_short_ratio"))
    if ratio is None:
        return _unavailable(name, bucket, "long_short_ratio missing")
    skew = ratio - Decimal("1")
    if abs(skew) < LONG_SHORT_SKEW_NEUTRAL_BAND:
        return _dim(name, bucket, "NEUTRAL", f"long/short ratio {ratio} roughly balanced")
    crowded_long = skew > 0
    crowded_with_us = (crowded_long and _sign(direction) > 0) or (not crowded_long and _sign(direction) < 0)
    if crowded_with_us:
        return _dim(name, bucket, "CONTRADICTS", f"long/short ratio {ratio} crowded in our own direction -- positioning risk")
    return _dim(name, bucket, "NEUTRAL", f"long/short ratio {ratio} skewed against our direction")


def _dim_liquidations(liquidations: dict, direction: str) -> dict:
    name, bucket = "liquidations_longs_shorts", "liquidation"
    if not liquidations.get("available"):
        return _unavailable(name, bucket, "liquidation data unavailable")
    imbalance = _d(liquidations.get("imbalance_15m"))
    if imbalance is None:
        return _unavailable(name, bucket, "imbalance_15m missing")
    cascade = bool(liquidations.get("cascade_detected"))
    if abs(imbalance) < LIQUIDATION_IMBALANCE_STRONG and not cascade:
        return _dim(name, bucket, "NEUTRAL", f"liquidation imbalance {imbalance} not significant")
    # imbalance > 0 => long-side liquidations dominant => current forced
    # selling pressure (momentum read -- confirms SHORT, contradicts LONG).
    long_dominant = imbalance > 0
    aligned = (long_dominant and _sign(direction) < 0) or (not long_dominant and _sign(direction) > 0)
    return _dim(name, bucket, "CONFIRMS" if aligned else "CONTRADICTS", f"liquidation imbalance {imbalance}, cascade={cascade}")


def _dim_orderbook_imbalance(orderbook: dict, direction: str) -> dict:
    name, bucket = "orderbook_imbalance", "orderbook"
    if not orderbook.get("available"):
        return _unavailable(name, bucket, "orderbook data unavailable")
    imbalance = _d(orderbook.get("imbalance"))
    if imbalance is None:
        return _unavailable(name, bucket, "orderbook imbalance missing")
    if abs(imbalance) < ORDERBOOK_IMBALANCE_STRONG:
        return _dim(name, bucket, "NEUTRAL", f"orderbook imbalance {imbalance} not significant")
    aligned = (imbalance > 0 and _sign(direction) > 0) or (imbalance < 0 and _sign(direction) < 0)
    return _dim(name, bucket, "CONFIRMS" if aligned else "CONTRADICTS", f"orderbook imbalance {imbalance}")


def _dim_spread_depth(orderbook: dict) -> dict:
    """Execution-quality dimension, not directional -- a wide spread works
    against ANY setup regardless of direction, but a tight spread is a
    precondition, not confirming evidence, so it never CONFIRMS on its
    own (same 'single snapshot is never a certain signal' principle)."""
    name, bucket = "spread_and_depth", "orderbook"
    if not orderbook.get("available"):
        return _unavailable(name, bucket, "orderbook data unavailable")
    spread_bps = _d(orderbook.get("spread_bps"))
    if spread_bps is None:
        return _unavailable(name, bucket, "spread_bps missing")
    if spread_bps >= SPREAD_BPS_POOR_EXECUTION:
        return _dim(name, bucket, "CONTRADICTS", f"spread {spread_bps}bps -- poor execution conditions")
    return _dim(name, bucket, "NEUTRAL", f"spread {spread_bps}bps within normal range")


def _dim_microprice(orderbook: dict, direction: str) -> dict:
    name, bucket = "microprice", "orderbook"
    if not orderbook.get("available"):
        return _unavailable(name, bucket, "orderbook data unavailable")
    micro, mid = _d(orderbook.get("microprice")), _d(orderbook.get("mid_price"))
    if micro is None or mid is None or mid == 0:
        return _unavailable(name, bucket, "microprice/mid_price missing")
    skew = (micro - mid) / mid
    if abs(skew) < MICROPRICE_SKEW_NEUTRAL_BAND:
        return _dim(name, bucket, "NEUTRAL", f"microprice skew {skew} negligible")
    aligned = (skew > 0 and _sign(direction) > 0) or (skew < 0 and _sign(direction) < 0)
    return _dim(name, bucket, "CONFIRMS" if aligned else "CONTRADICTS", f"microprice skew {skew}")


def _dim_large_trades(volume: dict, direction: str) -> dict:
    """large_trade_count carries no side information by itself -- only
    treated as directional when paired with the taker buy/sell split, and
    only when there's enough large-trade activity to say anything at
    all."""
    name, bucket = "large_trades", "volume"
    if not volume.get("available"):
        return _unavailable(name, bucket, "volume data unavailable")
    large_count = volume.get("large_trade_count")
    if large_count is None:
        return _unavailable(name, bucket, "large_trade_count missing")
    if int(large_count) < LARGE_TRADE_MIN_COUNT:
        return _dim(name, bucket, "NEUTRAL", f"only {large_count} large trades -- not enough activity to read")
    buy, sell = _d(volume.get("buy_taker_volume")), _d(volume.get("sell_taker_volume"))
    if buy is None or sell is None:
        return _dim(name, bucket, "NEUTRAL", f"{large_count} large trades but no side data available")
    aligned = (buy > sell and _sign(direction) > 0) or (sell > buy and _sign(direction) < 0)
    return _dim(name, bucket, "CONFIRMS" if aligned else "CONTRADICTS", f"{large_count} large trades, taker side leans {'buy' if buy > sell else 'sell'}")


def _dim_btc_alignment(symbol: str, direction: str, btc_regime: Optional[str]) -> dict:
    name, bucket = "btc_regime_alignment", "btc"
    if symbol == "BTCUSDT":
        return _dim(name, bucket, "NEUTRAL", "not applicable -- symbol IS BTCUSDT")
    if btc_regime is None:
        return _unavailable(name, bucket, "BTC regime context unavailable")
    if btc_regime in ("TREND_UP", "BREAKOUT_UP"):
        btc_dir = 1
    elif btc_regime in ("TREND_DOWN", "BREAKOUT_DOWN"):
        btc_dir = -1
    else:
        return _dim(name, bucket, "NEUTRAL", f"BTC regime {btc_regime} has no directional bias to compare against")
    return _dim(name, bucket, "CONFIRMS" if btc_dir == _sign(direction) else "CONTRADICTS", f"BTC regime {btc_regime}")


# Which dimension names depend on which `data_quality.stale_fields` source
# key (see feature_engine.assess_data_quality / regime_service_v2). A
# source being "available" (non-empty) is NOT the same as it being FRESH
# -- `available` only means "we have rows", staleness is a separate,
# time-based check already computed upstream. Stale sources are treated
# as UNAVAILABLE here, same as genuinely missing ones: an old CVD reading
# is not trustworthy evidence just because a row happens to exist.
_STALE_SOURCE_TO_DIMENSIONS = {
    "trades": {"cvd_direction_slope_divergence", "taker_buy_sell_ratio", "large_trades"},
    "orderbook": {"orderbook_imbalance", "spread_and_depth", "microprice"},
    "derivatives": {"open_interest_and_price_to_oi", "funding_extremity", "long_short_ratio"},
}


def confirm_setup(
    symbol: str,
    setup: dict,
    regime_full: dict,
    tf: Dict[str, dict],
    volume: dict,
    orderbook: dict,
    futures: dict,
    liquidations: dict,
    btc_regime: Optional[str] = None,
    data_quality: Optional[dict] = None,
) -> dict:
    """Evaluates order flow against an EXISTING Warstwa 2 setup. Returns
    NOT_APPLICABLE (not one of the 5 spec'd statuses -- there's simply
    nothing to confirm) if `setup` has no LONG/SHORT direction."""
    direction = setup.get("direction")
    if direction not in ("LONG", "SHORT"):
        return {
            "status": "NOT_APPLICABLE", "confidence": 0.0, "direction_evaluated": None,
            "dimensions": [], "used_by_regime": [], "used_by_setup": [],
            "new_independent_confirmations": [], "contradictions": [], "missing_sources": [],
            "net_independent_score": 0, "available_dimensions": 0, "total_dimensions": 0,
            "reasons": ["no active LONG/SHORT setup to confirm"],
        }

    used_by_regime = sorted(_used_buckets((regime_full.get("reasons") or []) + (regime_full.get("counterarguments") or [])))
    used_by_setup = sorted(_used_buckets((setup.get("reasons") or []) + (setup.get("counterarguments") or [])))
    already_used = set(used_by_regime) | set(used_by_setup)

    dims = [
        _dim_cvd(volume, direction),
        _dim_taker_ratio(volume, direction),
        _dim_oi(futures, direction),
        _dim_funding(futures, direction),
        _dim_long_short_ratio(futures, direction),
        _dim_liquidations(liquidations, direction),
        _dim_orderbook_imbalance(orderbook, direction),
        _dim_spread_depth(orderbook),
        _dim_microprice(orderbook, direction),
        _dim_large_trades(volume, direction),
        _dim_btc_alignment(symbol, direction, btc_regime),
    ]
    stale_fields = set((data_quality or {}).get("stale_fields") or [])
    for d in dims:
        d["already_used"] = d["bucket"] in already_used
        for src, names in _STALE_SOURCE_TO_DIMENSIONS.items():
            if src in stale_fields and d["dimension"] in names and d["read"] != "UNAVAILABLE":
                d["read"] = "UNAVAILABLE"
                d["detail"] = f"{d['detail']} -- OVERRIDDEN: {src} flagged stale, cannot trust this reading"

    available = [d for d in dims if d["read"] != "UNAVAILABLE"]
    missing_sources = sorted({d["dimension"] for d in dims if d["read"] == "UNAVAILABLE"})

    # Confirmations only count toward Warstwa 3's own confidence if NEITHER
    # upstream layer already used that feature bucket -- that's the whole
    # anti-double-counting rule. Contradictions always count: fresh risk
    # is always worth surfacing.
    independent_confirms = [d for d in dims if d["read"] == "CONFIRMS" and not d["already_used"]]
    contradicts = [d for d in dims if d["read"] == "CONTRADICTS"]
    new_independent_confirmations = [d["dimension"] for d in independent_confirms]
    contradictions = [d["dimension"] for d in contradicts]
    net_score = len(independent_confirms) - len(contradicts)

    if len(available) < MIN_AVAILABLE_DIMENSIONS_FOR_VERDICT:
        status = "INSUFFICIENT_DATA"
    elif net_score >= 3:
        status = "CONFIRMED"
    elif net_score >= 1:
        status = "WEAK_CONFIRMATION"
    elif net_score == 0:
        status = "NEUTRAL"
    else:
        status = "CONTRADICTED"

    base_confidence = {
        "CONFIRMED": 0.75, "WEAK_CONFIRMATION": 0.55, "NEUTRAL": 0.40,
        "CONTRADICTED": 0.25, "INSUFFICIENT_DATA": 0.15,
    }[status]
    confidence = round(max(0.05, min(0.90, base_confidence + 0.02 * max(0, net_score - 1))), 2)

    return {
        "status": status,
        "confidence": confidence,
        "direction_evaluated": direction,
        "dimensions": dims,
        "used_by_regime": used_by_regime,
        "used_by_setup": used_by_setup,
        "new_independent_confirmations": new_independent_confirmations,
        "contradictions": contradictions,
        "missing_sources": missing_sources,
        "net_independent_score": net_score,
        "available_dimensions": len(available),
        "total_dimensions": len(dims),
    }
