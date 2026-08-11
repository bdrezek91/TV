from __future__ import annotations

from pathlib import Path

from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_mean_reversion_v2_holdout import (
    MR_V2_HOLDOUT_DOWNLOAD_START,
    MR_V2_HOLDOUT_EVALUATION_END,
    MR_V2_HOLDOUT_EVALUATION_START,
    validate_frozen_v2_runtime_spec,
    validate_mr_v2_holdout_backfill,
)


def _summary(root: Path) -> dict:
    return {
        "symbols": list(REQUIRED_SYMBOLS),
        "window": {
            "evaluation_start": MR_V2_HOLDOUT_EVALUATION_START.isoformat(),
            "evaluation_end": MR_V2_HOLDOUT_EVALUATION_END.isoformat(),
            "download_start": MR_V2_HOLDOUT_DOWNLOAD_START.isoformat(),
            "download_end": MR_V2_HOLDOUT_EVALUATION_END.isoformat(),
            "evaluation_days": 120,
            "warmup_days": 10,
        },
        "results": {
            symbol: {
                "status": "OK",
                "manifest": {
                    "cache_dir": str(root / symbol),
                    "requested_start": MR_V2_HOLDOUT_DOWNLOAD_START.isoformat(),
                    "requested_end": MR_V2_HOLDOUT_EVALUATION_END.isoformat(),
                },
            }
            for symbol in REQUIRED_SYMBOLS
        },
    }


def test_runtime_spec_matches_pre_registration() -> None:
    report = validate_frozen_v2_runtime_spec()
    assert report["passed"] is True
    assert report["errors"] == []


def test_backfill_guard_accepts_exact_120d_window(tmp_path: Path) -> None:
    report = validate_mr_v2_holdout_backfill(_summary(tmp_path), tmp_path)
    assert report["passed"] is True
    assert report["errors"] == []


def test_backfill_guard_rejects_wrong_day_count(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary["window"]["evaluation_days"] = 119
    report = validate_mr_v2_holdout_backfill(summary, tmp_path)
    assert report["passed"] is False
    assert any("evaluation_days" in error for error in report["errors"])


def test_backfill_guard_rejects_symbol_order_change(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary["symbols"] = list(reversed(REQUIRED_SYMBOLS))
    report = validate_mr_v2_holdout_backfill(summary, tmp_path)
    assert report["passed"] is False
    assert any("symbols must exactly equal" in error for error in report["errors"])


def test_backfill_guard_rejects_cache_escape(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary["results"]["ETHUSDT"]["manifest"]["cache_dir"] = "/tmp/not-the-dedicated-root/ETHUSDT"
    report = validate_mr_v2_holdout_backfill(summary, tmp_path)
    assert report["passed"] is False
    assert any("escapes dedicated MR V2 holdout root" in error for error in report["errors"])
