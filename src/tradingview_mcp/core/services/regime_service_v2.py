"""DB-touching assembly layer for `regime_classifier_v2`.

Fetches already-persisted Bybit data via `query_repository` and reuses
`feature_engine`'s pure `compute_*_features` functions to build exactly
the inputs `regime_classifier_v2.classify_from_features` needs -- zero
reimplementation of feature math. Kept separate from the classifier so
the classifier itself stays unit-testable without a database.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.analysis import feature_engine as fe
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.database.repositories import query_repository as qr

# Historical-replay callers pass their own max_ages (see
# audit_regime_classifier.HIST_MAX_AGES) sized for replay's simulated
# "now" == cutoff, not wall-clock now -- these are for the LIVE path only.
LIVE_MAX_AGES = {
    # "candles" tracks the 1h close (see build_features_from_windows) --
    # a fresh 1h candle can legitimately be up to ~60min old, so the
    # threshold needs headroom above that, not the old 15m-cadence value.
    "candles": 4500.0,
    "trades": 120.0,
    "orderbook": 15.0,
    "derivatives": 600.0,
}


def candle_dict(c) -> dict:
    return {"open_time": c.open_time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}


def trade_agg_dict(r) -> dict:
    return {
        "bucket_start": r.bucket_start, "buy_taker_volume": r.buy_taker_volume,
        "sell_taker_volume": r.sell_taker_volume, "delta": r.delta, "trade_count": r.trade_count,
        "avg_trade_size": r.avg_trade_size, "large_trade_count": r.large_trade_count,
        "largest_trade_size": r.largest_trade_size,
    }


def liq_agg_dict(r) -> dict:
    return {
        "bucket_start": r.bucket_start, "long_liq_value": r.long_liq_value,
        "short_liq_value": r.short_liq_value, "long_liq_count": r.long_liq_count,
        "short_liq_count": r.short_liq_count,
    }


def orderbook_dict(r) -> dict:
    return {
        "source_timestamp": r.source_timestamp, "best_bid": r.best_bid, "best_ask": r.best_ask,
        "spread": r.spread, "spread_bps": r.spread_bps, "mid_price": r.mid_price,
        "microprice": r.microprice, "bid_depth": r.bid_depth, "ask_depth": r.ask_depth,
        "imbalance": r.imbalance, "is_consistent": r.is_consistent,
        "depth_bands": r.depth_bands_json,  # feature_engine reads "depth_bands", DB column is depth_bands_json
    }


def derivatives_dict(r) -> dict:
    return {
        "source_timestamp": r.source_timestamp, "open_interest": r.open_interest,
        "open_interest_value": r.open_interest_value, "funding_rate": r.funding_rate,
        "mark_price": r.mark_price, "index_price": r.index_price, "basis": r.basis,
        "basis_pct": r.basis_pct, "long_short_ratio": r.long_short_ratio,
    }


def build_features_from_windows(
    candles_4h: list, candles_1h: list, candles_15m: list,
    trade_aggs: list, liq_1m: list, liq_5m: list, liq_15m: list,
    ob_recent: list, deriv_recent: list,
    *, now: dt.datetime, max_ages: dict,
) -> dict:
    """Pure (no I/O) assembly of everything `classify_from_features` needs,
    given already-fetched, already-dict-shaped windows. Shared by both the
    live path and the historical-replay path in the research script, so
    both exercise the exact same feature math."""
    ob_latest = ob_recent[-1] if ob_recent else {}
    ob_previous = ob_recent[-2] if len(ob_recent) >= 2 else None
    deriv_latest = deriv_recent[-1] if deriv_recent else {}

    tf = {
        "4h": fe.compute_price_action_features(candles_4h),
        "1h": fe.compute_price_action_features(candles_1h),
        "15m": fe.compute_price_action_features(candles_15m),
    }
    volume = fe.compute_volume_features(trade_aggs)
    orderbook = fe.compute_orderbook_features(ob_latest, ob_previous, ob_recent)
    futures = fe.compute_futures_features(deriv_latest, deriv_recent)
    liquidations = fe.compute_liquidation_features(liq_1m, liq_5m, liq_15m)

    source_timestamps = {
        # "candles" tracks 1h (the finer of the two timeframes the
        # classifier's own UNSTABLE_DATA gate hard-requires -- see
        # regime_classifier_v2._data_quality_check, which now only
        # requires 4h+1h, not 15m). Using candles_15m here reintroduced
        # the exact same "15m retention is shorter than the replay window"
        # bug one layer up: is_tradeable would still collapse to False on
        # missing 15m even after the classifier's own check was relaxed.
        #
        # CLOSE time, not open_time: a candle's open_time marks the start
        # of its (still-forming) interval, not when its data became known.
        # Staleness measured against open_time makes a perfectly fresh
        # candle look up to one whole interval older than it really is
        # (worst case, right before the next candle closes, the gap between
        # "now" and open_time approaches ~2 intervals) -- this previously
        # caused live snapshots to flag fresh 1h candles as stale under a
        # threshold sized for a single interval.
        "candles": (candles_1h[-1]["open_time"] + dt.timedelta(hours=1)) if candles_1h else None,
        "trades": (trade_aggs[-1]["bucket_start"] + dt.timedelta(minutes=1)) if trade_aggs else None,
        "orderbook": ob_latest.get("source_timestamp") if ob_latest else None,
        "derivatives": deriv_latest.get("source_timestamp") if deriv_latest else None,
    }
    data_quality = fe.assess_data_quality(
        source_timestamps, max_ages,
        # Order book is an explicitly SECONDARY/supporting layer per the
        # roadmap, not a primary basis for classification -- it must not be
        # a *required* source. Its retention window is far shorter than the
        # candle history used in historical replay, so treating it as
        # required forced every historical point into UNSTABLE_DATA. Only
        # candles and trades gate is_tradeable; orderbook absence still
        # shows up in missing_fields / the data_quality_score, just doesn't
        # collapse is_tradeable on its own.
        required_sources=("candles", "trades"),
        orderbook_consistent=(ob_latest.get("is_consistent", True) if ob_latest else True),
        now=now,
    )
    return {"tf": tf, "volume": volume, "orderbook": orderbook, "futures": futures,
            "liquidations": liquidations, "data_quality": data_quality}


async def classify_symbol(
    session: AsyncSession,
    symbol: str,
    *,
    as_of: Optional[dt.datetime] = None,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
) -> dict:
    """Live classification: fetches the current window of each data source
    for `symbol` and classifies "as of now" (or `as_of`, for testing)."""
    as_of = as_of or dt.datetime.now(dt.timezone.utc)

    candles_4h = [candle_dict(c) for c in await qr.get_recent_candles(session, symbol, "240", limit=120)]
    candles_1h = [candle_dict(c) for c in await qr.get_recent_candles(session, symbol, "60", limit=120)]
    candles_15m = [candle_dict(c) for c in await qr.get_recent_candles(session, symbol, "15", limit=120)]

    trade_aggs = [trade_agg_dict(r) for r in await qr.get_recent_trade_aggregates(session, symbol, 60, limit=30)]

    # The collector only ever writes 1s/1m/5m liquidation buckets (see
    # aggregation.BUCKET_SECONDS) -- there is no 15m bucket in the DB.
    # compute_liquidation_features only cares about how many rows it's
    # handed, not their own bucket_seconds value, so the 1m/5m/15m
    # "windows" it wants are built here by slicing trailing 1/5/15 rows
    # out of the single 1-minute-bucket series, not by querying a bucket
    # size that doesn't exist.
    liq_1m_series = [liq_agg_dict(r) for r in await qr.get_recent_liquidation_aggregates(session, symbol, 60, limit=20)]
    liq_1m = liq_1m_series[-1:]
    liq_5m = liq_1m_series[-5:]
    liq_15m = liq_1m_series[-15:]

    ob_recent = [orderbook_dict(r) for r in await qr.get_recent_orderbook_snapshots(session, symbol, limit=20)]
    deriv_recent = [derivatives_dict(r) for r in await qr.get_recent_derivatives_snapshots(session, symbol, limit=20)]

    built = build_features_from_windows(
        candles_4h, candles_1h, candles_15m, trade_aggs, liq_1m, liq_5m, liq_15m,
        ob_recent, deriv_recent, now=as_of, max_ages=LIVE_MAX_AGES,
    )

    return rc.classify_from_features(
        symbol, as_of, built["tf"], built["volume"], built["orderbook"], built["futures"],
        built["liquidations"], built["data_quality"], btc_regime=btc_regime, previous_state=previous_state,
    )
