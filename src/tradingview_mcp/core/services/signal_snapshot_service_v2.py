"""Durable snapshot persistence for future outcome evaluation.

From Warstwa 3 onward, every live run that produces an actual LONG/SHORT
setup appends one JSONL record capturing the full regime -> setup ->
order-flow-confirmation decision chain, plus an `outcome` placeholder a
SEPARATE, LATER evaluator will fill in (price after 15m/1h/4h/12h, entry
touched, SL-or-TP-first, MFE, MAE). This module NEVER populates outcome
fields itself -- doing so here, at signal-generation time, would be
exactly the look-ahead bias Warstwa 1's historical replay already goes to
such lengths to avoid. Writing the signal and evaluating what happened
after it are deliberately two different code paths, run at two different
times.

Append-only JSONL under artifacts/research/ -- no DB, no migration,
matching every other research-track persistence choice made so far.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import List, Optional

SNAPSHOT_PATH = Path("/app/artifacts/research/signals/signals.jsonl")


def build_snapshots(
    symbol: str,
    as_of: dt.datetime,
    regime: dict,
    setups: dict,
    confirmations: dict,
    data_quality: dict,
    price_at_decision,
) -> List[dict]:
    """One record per setup_type that actually has a LONG/SHORT direction.
    NO_SETUP entries aren't persisted as trackable signals -- there is no
    entry/SL/TP to evaluate an outcome against."""
    records = []
    for setup_type, s in setups.items():
        if s.get("direction") not in ("LONG", "SHORT"):
            continue
        records.append({
            "snapshot_id": f"{symbol}_{setup_type}_{as_of.isoformat()}",
            "timestamp": as_of.isoformat(),
            "symbol": symbol,
            "regime": {
                "primary_regime": regime["primary_regime"], "confidence": regime["confidence"],
                "secondary_flags": regime.get("secondary_flags", []),
                "reasons": regime.get("reasons", []), "counterarguments": regime.get("counterarguments", []),
                "data_quality_score": regime.get("data_quality"),
            },
            "setup": {
                "setup_type": setup_type, "direction": s["direction"],
                "entry_zone": s["entry_zone"], "invalidation": s["invalidation"],
                "stop_loss": s["stop_loss"], "targets": s["targets"], "confidence": s["confidence"],
                "reasons": s["reasons"], "counterarguments": s["counterarguments"],
                "price_only_mode": s["price_only_mode"], "orderflow_confirmed": s["orderflow_confirmed"],
            },
            "orderflow_confirmation": confirmations.get(setup_type, {}),
            "data_quality_by_source": data_quality,
            "price_at_decision": price_at_decision,
            # Filled in later by a SEPARATE evaluator -- never at signal time.
            "outcome": {
                "price_after_15m": None, "price_after_1h": None, "price_after_4h": None, "price_after_12h": None,
                "entry_touched": None, "sl_or_tp_first": None, "mfe": None, "mae": None,
                "evaluated": False, "evaluated_at": None,
            },
        })
    return records


def append_snapshots(records: List[dict], path: Optional[Path] = None) -> None:
    if not records:
        return
    p = path or SNAPSHOT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
