"""DB-touching assembly layer for `setup_detector_v2` -- Warstwa 2.

Reuses `regime_service_v2.fetch_and_build_live_features` so a symbol's
setup evaluation runs against the EXACT same feature snapshot its regime
classification (Warstwa 1) used -- one DB round-trip, one consistent view
of the market at `as_of`, not two independently-timed fetches.

Pure detection only: `detect_setups_for_symbol` NEVER places, modifies,
or cancels an order. It returns candidate setups for a human or a later
risk-management layer to act on.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.analysis import setup_detector_v2 as sd
from tradingview_mcp.core.services import regime_service_v2 as rs


async def detect_setups_for_symbol(
    session: AsyncSession,
    symbol: str,
    *,
    as_of: Optional[dt.datetime] = None,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
) -> dict:
    """Classifies the current regime (Warstwa 1) then evaluates all 4
    setup types against it (Warstwa 2), on one shared feature snapshot.
    Returns a dict with both the full regime detail and the setups, plus
    `_state` for hysteresis threading by the caller (same convention as
    `regime_service_v2.classify_symbol`)."""
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    built = await rs.fetch_and_build_live_features(session, symbol, as_of=as_of)

    regime = rc.classify_from_features(
        symbol, as_of, built["tf"], built["volume"], built["orderbook"], built["futures"],
        built["liquidations"], built["data_quality"], btc_regime=btc_regime, previous_state=previous_state,
    )
    state = regime.pop("_state", None)

    result = sd.detect_setups(
        symbol, as_of, regime, built["tf"], built["volume"], built["orderbook"],
        built["futures"], built["liquidations"],
    )
    result["regime_full"] = regime
    result["_state"] = state
    return result
