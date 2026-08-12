"""Deterministic Feature Engine.

Computes the price-action / volume / order-book / futures / liquidations
feature groups from already-persisted Bybit data (candles, trade
aggregates, order-book feature snapshots, derivatives snapshots,
liquidation aggregates) plus a data-quality assessment that gates
downstream setup generation (`is_tradeable`).

Every function here is pure: it takes plain dicts/Decimals (the same shape
the Block 1 repository layer reads/writes) and returns plain dicts — no
database or network access, so the whole module is directly unit-testable
against fixture data.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from tradingview_mcp.core.analysis import indicators as ind

LARGE_TRADE_USD = Decimal("50000")


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


# -- Price action ------------------------------------------------------------

def compute_price_action_features(candles: Sequence[dict]) -> dict:
    """`candles` must be closed, chronologically ascending OHLCV dicts
    (as persisted in the `candles` table) for one interval (typically 15m)."""
    if not candles:
        return {"available": False}

    closes = [_dec(c["close"]) for c in candles]
    last_close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else None
    pct_change = (
        ((last_close - prev_close) / prev_close * Decimal("100"))
        if prev_close not in (None, Decimal("0"))
        else None
    )

    ema20 = ind.ema(closes, 20)
    ema50 = ind.ema(closes, 50)
    ema200 = ind.ema(closes, 200)

    def dist_pct(ema_val: Optional[Decimal]) -> Optional[Decimal]:
        if ema_val is None or ema_val == 0:
            return None
        return (last_close - ema_val) / ema_val * Decimal("100")

    sr = ind.local_support_resistance(candles)

    return {
        "available": True,
        "last_close": last_close,
        "pct_change": pct_change,
        "atr": ind.atr(candles, 14),
        "atr_pct": ind.atr_pct(candles, 14),
        "realized_volatility_pct": ind.realized_volatility(closes, 20),
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "distance_to_ema20_pct": dist_pct(ema20),
        "distance_to_ema50_pct": dist_pct(ema50),
        "distance_to_ema200_pct": dist_pct(ema200),
        "structure": ind.swing_structure(candles),
        "support": sr["support"],
        "resistance": sr["resistance"],
        "position_in_range": ind.position_in_range(candles),
        "vwap": ind.vwap(candles),
        "distance_to_vwap_pct": (
            (last_close - ind.vwap(candles)) / ind.vwap(candles) * Decimal("100")
            if ind.vwap(candles) not in (None, Decimal("0"))
            else None
        ),
    }


# -- Trades / volume / CVD ---------------------------------------------------

def compute_volume_features(trade_aggregates_1m: Sequence[dict]) -> dict:
    """`trade_aggregates_1m` must be chronologically ascending 1-minute
    `trade_aggregates` rows (as persisted by the collector)."""
    if not trade_aggregates_1m:
        return {"available": False}

    deltas = [_dec(r["delta"]) for r in trade_aggregates_1m]
    cvd_series: List[Decimal] = []
    running = Decimal("0")
    for d in deltas:
        running += d
        cvd_series.append(running)
    cvd = cvd_series[-1]

    def delta_over(n: int) -> Optional[Decimal]:
        if len(deltas) < n:
            return None
        return sum(deltas[-n:], Decimal("0"))

    def cvd_slope(n: int) -> Optional[Decimal]:
        if len(cvd_series) < n:
            return None
        return cvd_series[-1] - cvd_series[-n]

    total_sizes = [_dec(r.get("avg_trade_size", 0)) * _dec(r.get("trade_count", 0)) for r in trade_aggregates_1m]
    trade_counts = [int(r.get("trade_count", 0)) for r in trade_aggregates_1m]
    total_trades = sum(trade_counts)
    total_volume = sum(total_sizes, Decimal("0"))
    avg_trade_size = (total_volume / total_trades) if total_trades else Decimal("0")

    volumes = [_dec(r.get("buy_taker_volume", 0)) + _dec(r.get("sell_taker_volume", 0)) for r in trade_aggregates_1m]
    volume_z = _z_score(volumes)

    large_trade_count = sum(int(r.get("large_trade_count", 0)) for r in trade_aggregates_1m)
    largest_trade = max((_dec(r.get("largest_trade_size", 0)) for r in trade_aggregates_1m), default=Decimal("0"))

    return {
        "available": True,
        "buy_taker_volume": sum((_dec(r.get("buy_taker_volume", 0)) for r in trade_aggregates_1m), Decimal("0")),
        "sell_taker_volume": sum((_dec(r.get("sell_taker_volume", 0)) for r in trade_aggregates_1m), Decimal("0")),
        "delta": sum(deltas, Decimal("0")),
        "cvd": cvd,
        "cvd_delta_1m": delta_over(1),
        "cvd_delta_5m": delta_over(5),
        "cvd_delta_15m": delta_over(15),
        "cvd_slope_5m": cvd_slope(5),
        "cvd_slope_15m": cvd_slope(15),
        "volume_zscore": volume_z,
        "avg_trade_size": avg_trade_size,
        "large_trade_count": large_trade_count,
        "largest_trade_size": largest_trade,
    }


def _z_score(values: Sequence[Decimal]) -> Optional[Decimal]:
    if len(values) < 2:
        return None
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(len(values))
    if variance == 0:
        return Decimal("0")
    std = variance.sqrt()
    return (values[-1] - mean) / std


# -- Order book ---------------------------------------------------------------

def compute_orderbook_features(
    latest: dict,
    previous: Optional[dict] = None,
    recent: Optional[Sequence[dict]] = None,
) -> dict:
    """`latest`/`previous` are `orderbook_feature_snapshots`-shaped dicts
    (as produced by `core.market_data.bybit.orderbook.LocalOrderBook.stats`).
    `recent` is an optional chronological window used for persistence.

    A single large wall is never treated as a standalone signal here: this
    function only ever reports depth/imbalance *aggregates* and a
    `large_wall_present` boolean informational flag — nothing in the
    Scoring Engine (`scoring.py`) reads that flag as a component on its
    own, only as a footnote alongside `order_book` imbalance/depth.
    """
    if not latest:
        return {"available": False}

    imbalance = latest.get("imbalance")
    imbalance_change = None
    if previous is not None and previous.get("imbalance") is not None and imbalance is not None:
        imbalance_change = _dec(imbalance) - _dec(previous["imbalance"])

    persistence = None
    if recent:
        signs = [
            (1 if _dec(r["imbalance"]) > 0 else (-1 if _dec(r["imbalance"]) < 0 else 0))
            for r in recent
            if r.get("imbalance") is not None
        ]
        if signs:
            current_sign = signs[-1]
            streak = 0
            for s in reversed(signs):
                if s == current_sign:
                    streak += 1
                else:
                    break
            persistence = streak

    replenishment = None
    if recent and len(recent) >= 2:
        depths = [_dec(r.get("bid_depth", 0)) + _dec(r.get("ask_depth", 0)) for r in recent if r.get("bid_depth") is not None]
        if len(depths) >= 2:
            replenishment = depths[-1] - depths[-2]

    bid_depth = _dec(latest.get("bid_depth", 0))
    ask_depth = _dec(latest.get("ask_depth", 0))
    large_wall_present = bool(
        (bid_depth > 0 and bid_depth > ask_depth * Decimal("3"))
        or (ask_depth > 0 and ask_depth > bid_depth * Decimal("3"))
    )

    # Absorption: large opposing-side volume traded through a level without
    # the level's depth collapsing (heuristic: depth held roughly flat
    # despite non-zero replenishment activity — flagged, not scored alone).
    absorption_detected = replenishment is not None and abs(replenishment) < (bid_depth + ask_depth) * Decimal("0.05")

    return {
        "available": True,
        "best_bid": latest.get("best_bid"),
        "best_ask": latest.get("best_ask"),
        "spread": latest.get("spread"),
        "spread_bps": latest.get("spread_bps"),
        "mid_price": latest.get("mid_price"),
        "microprice": latest.get("microprice"),
        "bid_depth": latest.get("bid_depth"),
        "ask_depth": latest.get("ask_depth"),
        "imbalance": imbalance,
        "imbalance_change": imbalance_change,
        "imbalance_persistence_snapshots": persistence,
        "replenishment": replenishment,
        "absorption_detected": absorption_detected,
        "large_wall_present": large_wall_present,
        "depth_bands": latest.get("depth_bands", {}),
        "is_consistent": latest.get("is_consistent", False),
    }


# -- Futures / derivatives -----------------------------------------------------

def compute_futures_features(latest: dict, history: Sequence[dict]) -> dict:
    """`history` is chronologically ascending `derivatives_snapshots` rows;
    `latest` is the most recent one (may be `history[-1]`)."""
    if not latest:
        return {"available": False}

    def oi_change_over(minutes: int) -> Optional[Decimal]:
        if not history or latest.get("open_interest") is None:
            return None
        now_ts = latest.get("source_timestamp") or history[-1].get("source_timestamp")
        if now_ts is None:
            return None
        cutoff = now_ts - dt.timedelta(minutes=minutes)
        candidates = [h for h in history if h.get("source_timestamp") and h["source_timestamp"] <= cutoff and h.get("open_interest") is not None]
        if not candidates:
            return None
        baseline = _dec(candidates[-1]["open_interest"])
        if baseline == 0:
            return None
        return (_dec(latest["open_interest"]) - baseline) / baseline * Decimal("100")

    price_to_oi = None
    if len(history) >= 2 and history[0].get("mark_price") and history[-1].get("mark_price") and history[0].get("open_interest") and history[-1].get("open_interest"):
        price_chg = _dec(history[-1]["mark_price"]) - _dec(history[0]["mark_price"])
        oi_chg = _dec(history[-1]["open_interest"]) - _dec(history[0]["open_interest"])
        if price_chg > 0 and oi_chg > 0:
            price_to_oi = "PRICE_UP_OI_UP"       # new longs entering — trend-confirming
        elif price_chg > 0 and oi_chg < 0:
            price_to_oi = "PRICE_UP_OI_DOWN"     # short covering — weaker confirmation
        elif price_chg < 0 and oi_chg > 0:
            price_to_oi = "PRICE_DOWN_OI_UP"     # new shorts entering — trend-confirming
        elif price_chg < 0 and oi_chg < 0:
            price_to_oi = "PRICE_DOWN_OI_DOWN"   # long liquidation/close — weaker confirmation
        else:
            price_to_oi = "FLAT"

    basis_change = None
    if len(history) >= 2 and history[0].get("basis") is not None and history[-1].get("basis") is not None:
        basis_change = _dec(history[-1]["basis"]) - _dec(history[0]["basis"])

    return {
        "available": True,
        "open_interest": latest.get("open_interest"),
        "oi_change_5m_pct": oi_change_over(5),
        "oi_change_15m_pct": oi_change_over(15),
        "oi_change_1h_pct": oi_change_over(60),
        "funding_rate": latest.get("funding_rate"),
        "mark_price": latest.get("mark_price"),
        "index_price": latest.get("index_price"),
        "basis": latest.get("basis"),
        "basis_change": basis_change,
        "long_short_ratio": latest.get("long_short_ratio"),
        "price_to_oi_relation": price_to_oi,
    }


# -- Liquidations ---------------------------------------------------------------

def compute_liquidation_features(
    liq_1m: Sequence[dict], liq_5m: Sequence[dict], liq_15m: Sequence[dict]
) -> dict:
    def summarize(rows: Sequence[dict]) -> dict:
        if not rows:
            return {"long_value": Decimal("0"), "short_value": Decimal("0")}
        long_v = sum((_dec(r.get("long_liq_value", 0)) for r in rows), Decimal("0"))
        short_v = sum((_dec(r.get("short_liq_value", 0)) for r in rows), Decimal("0"))
        return {"long_value": long_v, "short_value": short_v}

    s1, s5, s15 = summarize(liq_1m), summarize(liq_5m), summarize(liq_15m)
    total = s15["long_value"] + s15["short_value"]
    imbalance = ((s15["long_value"] - s15["short_value"]) / total) if total else Decimal("0")

    totals_series = [_dec(r.get("long_liq_value", 0)) + _dec(r.get("short_liq_value", 0)) for r in liq_1m]
    liq_zscore = _z_score(totals_series)

    # Cascade: 3+ consecutive 1m buckets each above a fixed absolute
    # threshold, in the same direction.
    cascade = False
    threshold = Decimal("100000")
    if len(liq_1m) >= 3:
        recent3 = liq_1m[-3:]
        long_streak = all(_dec(r.get("long_liq_value", 0)) >= threshold for r in recent3)
        short_streak = all(_dec(r.get("short_liq_value", 0)) >= threshold for r in recent3)
        cascade = bool(long_streak or short_streak)

    return {
        "available": bool(liq_1m or liq_5m or liq_15m),
        "long_liq_value_1m": s1["long_value"],
        "short_liq_value_1m": s1["short_value"],
        "long_liq_value_5m": s5["long_value"],
        "short_liq_value_5m": s5["short_value"],
        "long_liq_value_15m": s15["long_value"],
        "short_liq_value_15m": s15["short_value"],
        "imbalance_15m": imbalance,
        "zscore_1m": liq_zscore,
        "cascade_detected": cascade,
    }


# -- Data quality / tradeability gate --------------------------------------------

def assess_data_quality(
    source_timestamps: Dict[str, Optional[dt.datetime]],
    max_ages: Dict[str, float],
    *,
    required_sources: Sequence[str] = ("candles", "trades", "orderbook"),
    orderbook_consistent: bool = True,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Combines per-source freshness into one data-quality gate.

    `source_timestamps` maps a source name (e.g. "candles", "trades",
    "orderbook", "derivatives") to its most recent known timestamp (or
    None if entirely missing). `max_ages` maps the same names to their
    staleness threshold in seconds.

    `is_tradeable` is False whenever any *required* source is missing or
    stale, or the order book is flagged inconsistent — matching the plan's
    "nie generuj wtedy setupu" rule.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    missing_fields: List[str] = []
    stale_fields: List[str] = []
    scored_sources = 0
    healthy_sources = 0

    for name, ts in source_timestamps.items():
        scored_sources += 1
        if ts is None:
            missing_fields.append(name)
            continue
        max_age = max_ages.get(name, 60)
        age = (now - ts).total_seconds()
        if age > max_age:
            stale_fields.append(name)
        else:
            healthy_sources += 1

    data_quality_score = int(round((healthy_sources / scored_sources) * 100)) if scored_sources else 0

    critical_bad = any(s in missing_fields or s in stale_fields for s in required_sources)
    is_tradeable = (not critical_bad) and orderbook_consistent

    return {
        "data_quality_score": data_quality_score,
        "missing_fields": missing_fields,
        "stale_fields": stale_fields,
        "source_timestamps": {k: (v.isoformat() if v else None) for k, v in source_timestamps.items()},
        "is_tradeable": is_tradeable,
    }


def build_feature_snapshot(
    symbol: str,
    *,
    candles_15m: Sequence[dict],
    trade_aggregates_1m: Sequence[dict],
    orderbook_latest: dict,
    orderbook_previous: Optional[dict],
    orderbook_recent: Sequence[dict],
    derivatives_latest: dict,
    derivatives_history: Sequence[dict],
    liq_1m: Sequence[dict],
    liq_5m: Sequence[dict],
    liq_15m: Sequence[dict],
    max_ages: Dict[str, float],
    now: Optional[dt.datetime] = None,
) -> dict:
    """Assembles the full feature snapshot persisted to `feature_snapshots`."""
    now = now or dt.datetime.now(dt.timezone.utc)

    price_action = compute_price_action_features(candles_15m)
    volume = compute_volume_features(trade_aggregates_1m)
    orderbook = compute_orderbook_features(orderbook_latest, orderbook_previous, orderbook_recent)
    futures = compute_futures_features(derivatives_latest, derivatives_history)
    liquidations = compute_liquidation_features(liq_1m, liq_5m, liq_15m)

    source_timestamps = {
        "candles": candles_15m[-1]["open_time"] if candles_15m else None,
        "trades": trade_aggregates_1m[-1]["bucket_start"] if trade_aggregates_1m else None,
        "orderbook": orderbook_latest.get("source_timestamp") if orderbook_latest else None,
        "derivatives": derivatives_latest.get("source_timestamp") if derivatives_latest else None,
    }
    quality = assess_data_quality(
        source_timestamps,
        max_ages,
        orderbook_consistent=bool(orderbook_latest and orderbook_latest.get("is_consistent", False)),
        now=now,
    )

    return {
        "symbol": symbol,
        "as_of": now,
        "price_action": price_action,
        "volume": volume,
        "orderbook": orderbook,
        "futures": futures,
        "liquidations": liquidations,
        **quality,
    }
