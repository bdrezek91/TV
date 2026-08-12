"""Durable snapshot persistence for future outcome evaluation.

From Warstwa 3 onward, every live run that produces an actual LONG/SHORT
setup appends one JSONL record capturing the full regime -> setup ->
order-flow-confirmation -> risk-decision chain, plus an `outcome`
placeholder a SEPARATE, LATER evaluator will fill in (price after
15m/1h/4h/12h, entry touched, SL-or-TP-first, MFE, MAE). This module
NEVER populates outcome fields itself -- doing so here, at signal-
generation time, would be exactly the look-ahead bias Warstwa 1's
historical replay already goes to such lengths to avoid. Writing the
signal and evaluating what happened after it are deliberately two
different code paths, run at two different times.

`signal_id` is deliberately NOT built from wall-clock `as_of` directly
(that would make every run produce a "new" id even for the same
underlying market moment, defeating deduplication). It's built from:
  symbol + setup_type + direction + decision_hour_bucket + regime_timestamp
`decision_hour_bucket` is `as_of` truncated to the hour, and
`regime_timestamp` is the underlying 1h-candle-anchored timestamp
(`data_quality.source_timestamps["candles"]`) that only changes once a
new 1h candle actually closes. Re-running the audit script (or
restarting the process) multiple times within the same hour against the
same underlying candle therefore produces the SAME signal_id, and
`append_snapshots` skips anything already on disk.

Append-only JSONL under artifacts/research/ -- no DB, no migration,
matching every other research-track persistence choice made so far.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Dict, List, Optional

SNAPSHOT_PATH = Path("/app/artifacts/research/signals/signals.jsonl")


def make_signal_id(symbol: str, setup_type: str, direction: str, as_of: dt.datetime, regime_timestamp) -> str:
    decision_bucket = as_of.replace(minute=0, second=0, microsecond=0).isoformat()
    return f"{symbol}_{setup_type}_{direction}_{decision_bucket}_{regime_timestamp}"


def build_snapshots(
    symbol: str,
    as_of: dt.datetime,
    regime: dict,
    setups: dict,
    confirmations: dict,
    risk_decisions: dict,
    data_quality: dict,
    price_at_decision,
) -> List[dict]:
    """One record per setup_type that actually has a LONG/SHORT direction.
    NO_SETUP entries aren't persisted as trackable signals -- there is no
    entry/SL/TP to evaluate an outcome against."""
    regime_timestamp = (data_quality.get("source_timestamps") or {}).get("candles")
    records = []
    for setup_type, s in setups.items():
        if s.get("direction") not in ("LONG", "SHORT"):
            continue
        risk = risk_decisions.get(setup_type, {})
        records.append({
            "snapshot_id": make_signal_id(symbol, setup_type, s["direction"], as_of, regime_timestamp),
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
            "risk_decision": {
                "decision": risk.get("decision"), "position_size": risk.get("position_size"),
                "position_value": risk.get("position_value"), "risk_amount": risk.get("risk_amount"),
                "stop_loss": risk.get("stop_loss"), "targets": risk.get("targets"),
                "reward_to_risk": risk.get("reward_to_risk"), "expected_fees": risk.get("expected_fees"),
                "estimated_slippage_bps": risk.get("estimated_slippage_bps"),
                "portfolio_limits_used": risk.get("portfolio_limits_used"),
                "reasons": risk.get("reasons"), "rejection_reasons": risk.get("rejection_reasons"),
                "activation_conditions": risk.get("activation_conditions"),
                "config_version": risk.get("config_version"),
            },
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


def load_existing_signal_ids(path: Optional[Path] = None) -> set:
    p = path or SNAPSHOT_PATH
    if not p.exists():
        return set()
    ids = set()
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["snapshot_id"])
            except Exception:
                continue
    return ids


def append_snapshots(records: List[dict], path: Optional[Path] = None) -> dict:
    """Skips any record whose snapshot_id already exists on disk --
    protects against double-writing the same signal on a process restart
    or a re-run of the audit script within the same decision hour."""
    p = path or SNAPSHOT_PATH
    existing = load_existing_signal_ids(p)
    new_records = [r for r in records if r["snapshot_id"] not in existing]
    duplicates_skipped = len(records) - len(new_records)
    if new_records:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            for r in new_records:
                f.write(json.dumps(r, default=str) + "\n")
    return {"written": len(new_records), "duplicates_skipped": duplicates_skipped}


def update_snapshot_outcomes(outcome_updates: Dict[str, dict], path: Optional[Path] = None) -> dict:
    """Merges Warstwa 5 paper-execution results into each EXISTING
    snapshot's `outcome` sub-object, in place -- never appends a new,
    independent record for a signal that already has one, and never
    touches `regime`/`setup`/`orderflow_confirmation`/`risk_decision`
    (Warstwa 1-4's original decision data is immutable once written).
    `outcome_updates` maps signal_id -> partial outcome dict to merge.
    Rewrites the whole file (research-scale JSONL, not a hot path) since
    JSONL has no native in-place update."""
    p = path or SNAPSHOT_PATH
    if not p.exists() or not outcome_updates:
        return {"updated": 0, "not_found": list(outcome_updates.keys())}
    lines = p.read_text().splitlines()
    updated = 0
    found_ids = set()
    new_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        sid = record.get("snapshot_id")
        if sid in outcome_updates:
            record["outcome"] = dict(record.get("outcome") or {}, **outcome_updates[sid])
            updated += 1
            found_ids.add(sid)
        new_lines.append(json.dumps(record, default=str))
    p.write_text("\n".join(new_lines) + ("\n" if new_lines else ""))
    not_found = [sid for sid in outcome_updates if sid not in found_ids]
    return {"updated": updated, "not_found": not_found}
