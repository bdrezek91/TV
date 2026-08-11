"""Pre-registered untouched historical OOS contract for Mean Reversion V2.

MR V2 was derived only after MR V1 failed its 2026-04-12..2026-07-10 holdout,
and was then checked on already-observed 2026-07-11..2026-08-09 development
data.  This module freezes the next validation *before* its data are downloaded
or outcomes inspected.

The holdout is historically earlier than development (therefore it is not a
forward test), but it is a strictly non-overlapping, previously unobserved OOS
sample.  A later live/shadow forward test is still required after any PASS.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from tradingview_mcp.core.backtest.comparison_holdout_v1 import (
    PASS_RULE as V1_PASS_RULE,
    REQUIRED_SYMBOLS,
    assess_holdout,
)
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import DEFAULT_TARGET_DUST_FRACTION
from tradingview_mcp.core.backtest.comparison_mean_reversion_v2 import (
    MR_V2_MAX_OI_CHANGE_1H_PCT,
    MR_V2_NAME,
    MR_V2_REJECT_15M_STRUCTURE,
    MR_V2_REJECT_4H_STRUCTURE,
)

UTC = dt.timezone.utc

MR_V2_HOLDOUT_EVALUATION_START = dt.datetime(2025, 12, 13, 0, 0, tzinfo=UTC)
MR_V2_HOLDOUT_EVALUATION_END = dt.datetime(2026, 4, 11, 23, 59, 59, 999999, tzinfo=UTC)
MR_V2_HOLDOUT_EVALUATION_DAYS = 120
MR_V2_HOLDOUT_WARMUP_DAYS = 10
MR_V2_HOLDOUT_DOWNLOAD_START = MR_V2_HOLDOUT_EVALUATION_START - dt.timedelta(days=MR_V2_HOLDOUT_WARMUP_DAYS)
MR_V2_DEVELOPMENT_START = dt.datetime(2026, 4, 12, 0, 0, tzinfo=UTC)
MR_V2_MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
MR_V2_HOLDOUT_DECISION_END = MR_V2_HOLDOUT_EVALUATION_END - MR_V2_MIN_FUTURE_EXECUTION

# Dedicated root is intentionally different from both the original discovery
# cache and MR V1 holdout cache.
DEFAULT_MR_V2_HOLDOUT_CACHE_ROOT = Path(
    "/app/artifacts/research/backtest_comparison/holdout_mrv2_120d/cache"
)
DEFAULT_MR_V2_HOLDOUT_OUTPUT = Path(
    "/app/artifacts/research/backtest_comparison/holdout_mrv2_120d/"
    "research_v2_mean_reversion_v2_holdout_120d.json"
)

# Keep the same robustness rule used for MR V1.  The longer 120d horizon was
# selected *before* outcomes to give the more selective V2 a fair chance to
# satisfy the existing >=100 FULL_24H_1H terminal-trade requirement without
# lowering that requirement.
PASS_RULE = dict(V1_PASS_RULE)

# Git blob ids of the exact research implementation frozen at pre-registration.
# These are provenance markers in the immutable report.
FROZEN_V2_SOURCE_BLOBS = {
    "regime_classifier_v2.py": "b7917da5637f8c28936ef1f9d6dd7ae3ed3b60ad",
    "setup_detector_v2.py": "bd63a82368bf394dd245e4a4f419f04e25bb4aa1",
    "orderflow_confirmation_v2.py": "d9eeca51310f811ccf4e5995833b26332f630300",
    "risk_manager_v2.py": "6812a76e374658eb114f9af89ba0fff66edeecb7",
    "comparison_candidate_v1.py": "82fe37fe0731cf8cdef567f70b23ec6471e055ca",
    "comparison_execution_v1.py": "5f174f91ec83ba33cec19a407fbe9ee9846861be",
    "comparison_mean_reversion_v2.py": "1efd87a302f4693bf425e60f4a4d033b6c18ae24",
    "comparison_lifecycle_v1.py": "334ee21dfe3f49ee5116463cf4f71ed55b35ca9b",
}

FROZEN_V2_SPEC = {
    "candidate": "MR_V2_EXHAUSTION_RANGE_QUALITY",
    "existing_setup": "mean_reversion",
    "reject_15m_structure": "CONTRACTING",
    "reject_4h_structure": "EXPANDING",
    "maximum_oi_change_1h_pct": Decimal("0"),
    "futures_unavailable_policy": "FAIL_CLOSED",
    "symbol_filter": None,
    "hour_filter": None,
    "direction_filter": None,
    "w3_status_filter": None,
    "cost_filter": None,
    "lifecycle_target_dust_repair": True,
    "target_dust_fraction": Decimal("1e-12"),
}


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out.astimezone(UTC)


def validate_frozen_v2_runtime_spec() -> dict[str, Any]:
    """Fail closed if imported V2 constants drift from pre-registration."""
    errors: list[str] = []
    if MR_V2_NAME != FROZEN_V2_SPEC["candidate"]:
        errors.append(f"MR_V2_NAME drifted: {MR_V2_NAME}")
    if MR_V2_REJECT_15M_STRUCTURE != FROZEN_V2_SPEC["reject_15m_structure"]:
        errors.append("15m structure gate drifted")
    if MR_V2_REJECT_4H_STRUCTURE != FROZEN_V2_SPEC["reject_4h_structure"]:
        errors.append("4h structure gate drifted")
    if MR_V2_MAX_OI_CHANGE_1H_PCT != FROZEN_V2_SPEC["maximum_oi_change_1h_pct"]:
        errors.append("OI threshold drifted")
    if DEFAULT_TARGET_DUST_FRACTION != FROZEN_V2_SPEC["target_dust_fraction"]:
        errors.append("lifecycle dust threshold drifted")
    return {"passed": not errors, "errors": errors, "frozen_spec": FROZEN_V2_SPEC}


def validate_mr_v2_holdout_backfill(summary: Mapping[str, Any], cache_root: Path) -> dict[str, Any]:
    """Fail closed unless cache metadata exactly matches the 120d contract."""
    window = dict(summary.get("window") or {})
    errors: list[str] = []

    try:
        actual_start = _dt(window.get("evaluation_start"))
        actual_end = _dt(window.get("evaluation_end"))
        actual_download_start = _dt(window.get("download_start"))
        actual_download_end = _dt(window.get("download_end"))
    except Exception as exc:
        return {"passed": False, "errors": [f"invalid/missing backfill window: {exc}"]}

    if actual_start != MR_V2_HOLDOUT_EVALUATION_START:
        errors.append(
            f"evaluation_start {actual_start.isoformat()} != frozen "
            f"{MR_V2_HOLDOUT_EVALUATION_START.isoformat()}"
        )
    if actual_end != MR_V2_HOLDOUT_EVALUATION_END:
        errors.append(
            f"evaluation_end {actual_end.isoformat()} != frozen "
            f"{MR_V2_HOLDOUT_EVALUATION_END.isoformat()}"
        )
    if actual_download_start != MR_V2_HOLDOUT_DOWNLOAD_START:
        errors.append(
            f"download_start {actual_download_start.isoformat()} != frozen "
            f"{MR_V2_HOLDOUT_DOWNLOAD_START.isoformat()}"
        )
    if actual_download_end != MR_V2_HOLDOUT_EVALUATION_END:
        errors.append("download_end does not equal frozen evaluation_end")
    if int(window.get("evaluation_days") or 0) != MR_V2_HOLDOUT_EVALUATION_DAYS:
        errors.append(f"evaluation_days must equal {MR_V2_HOLDOUT_EVALUATION_DAYS}")
    if int(window.get("warmup_days") or 0) != MR_V2_HOLDOUT_WARMUP_DAYS:
        errors.append(f"warmup_days must equal {MR_V2_HOLDOUT_WARMUP_DAYS}")
    if actual_end >= MR_V2_DEVELOPMENT_START:
        errors.append("MR V2 holdout overlaps already-observed development data")

    summary_symbols = tuple(str(s).upper() for s in (summary.get("symbols") or []))
    if summary_symbols != REQUIRED_SYMBOLS:
        errors.append(f"symbols must exactly equal frozen universe {REQUIRED_SYMBOLS}, got {summary_symbols}")

    results = dict(summary.get("results") or {})
    root_resolved = cache_root.resolve()
    for symbol in REQUIRED_SYMBOLS:
        row = results.get(symbol) or {}
        manifest = row.get("manifest") or {}
        cache_dir_raw = manifest.get("cache_dir")
        if row.get("status") != "OK" or not cache_dir_raw:
            errors.append(f"{symbol} missing successful cache_dir")
            continue
        cache_dir = Path(cache_dir_raw).resolve()
        try:
            cache_dir.relative_to(root_resolved)
        except ValueError:
            errors.append(f"{symbol} cache_dir escapes dedicated MR V2 holdout root: {cache_dir}")
        if manifest.get("requested_start") and _dt(manifest["requested_start"]) != MR_V2_HOLDOUT_DOWNLOAD_START:
            errors.append(f"{symbol} manifest requested_start differs from frozen download start")
        if manifest.get("requested_end") and _dt(manifest["requested_end"]) != MR_V2_HOLDOUT_EVALUATION_END:
            errors.append(f"{symbol} manifest requested_end differs from frozen download end")

    runtime = validate_frozen_v2_runtime_spec()
    errors.extend(runtime["errors"])
    return {
        "passed": not errors,
        "errors": errors,
        "evaluation_start": MR_V2_HOLDOUT_EVALUATION_START,
        "evaluation_end": MR_V2_HOLDOUT_EVALUATION_END,
        "decision_end": MR_V2_HOLDOUT_DECISION_END,
        "development_start": MR_V2_DEVELOPMENT_START,
        "evaluation_days": MR_V2_HOLDOUT_EVALUATION_DAYS,
        "warmup_days": MR_V2_HOLDOUT_WARMUP_DAYS,
        "symbols": list(REQUIRED_SYMBOLS),
        "runtime_spec": runtime,
    }


def assess_mr_v2_holdout(results: Mapping[str, Any]) -> dict[str, Any]:
    """Apply exactly the same pre-registered aggregate robustness rule as V1."""
    return assess_holdout(results)
