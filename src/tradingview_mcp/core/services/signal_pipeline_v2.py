"""Runs regime -> setup -> order-flow-confirmation for one symbol on a
single shared feature snapshot -- Warstwa 1 + 2 + 3 chained together.

NEVER places, modifies, or cancels a real order. This is the read path
only: classify, detect, confirm, describe. Order execution (paper or
real) is explicitly a later layer's job.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.analysis import setup_detector_v2 as sd
from tradingview_mcp.core.services import regime_service_v2 as rs


async def run_pipeline_for_symbol(
    session: AsyncSession,
    symbol: str,
    *,
    as_of: Optional[dt.datetime] = None,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
) -> dict:
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    built = await rs.fetch_and_build_live_features(session, symbol, as_of=as_of)

    regime = rc.classify_from_features(
        symbol, as_of, built["tf"], built["volume"], built["orderbook"], built["futures"],
        built["liquidations"], built["data_quality"], btc_regime=btc_regime, previous_state=previous_state,
    )
    state = regime.pop("_state", None)

    setup_result = sd.detect_setups(
        symbol, as_of, regime, built["tf"], built["volume"], built["orderbook"],
        built["futures"], built["liquidations"],
    )
    setups = setup_result["setups"]

    confirmations = {}
    for setup_type, s in setups.items():
        confirmations[setup_type] = ofc.confirm_setup(
            symbol, s, regime, built["tf"], built["volume"], built["orderbook"], built["futures"],
            built["liquidations"], btc_regime=btc_regime, data_quality=built["data_quality"],
        )

    pa_1h = built["tf"].get("1h") or {}
    pa_4h = built["tf"].get("4h") or {}
    price_at_decision = pa_1h.get("last_close") if pa_1h.get("available") else pa_4h.get("last_close")

    return {
        "symbol": symbol,
        "as_of": as_of,
        "regime": regime,
        "setups": setups,
        "confirmations": confirmations,
        "data_quality": built["data_quality"],
        "price_at_decision": price_at_decision,
        "_state": state,
    }
