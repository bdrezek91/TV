from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tradingview_mcp.core.backtest.comparison_holdout_v1 import (
    HOLDOUT_DOWNLOAD_START,
    HOLDOUT_EVALUATION_END,
    HOLDOUT_EVALUATION_START,
    REQUIRED_SYMBOLS,
    assess_holdout,
    validate_holdout_backfill,
)


def _summary(root: Path) -> dict:
    return {
        "symbols": list(REQUIRED_SYMBOLS),
        "window": {
            "evaluation_start": HOLDOUT_EVALUATION_START.isoformat(),
            "evaluation_end": HOLDOUT_EVALUATION_END.isoformat(),
            "download_start": HOLDOUT_DOWNLOAD_START.isoformat(),
            "download_end": HOLDOUT_EVALUATION_END.isoformat(),
            "evaluation_days": 90,
            "warmup_days": 10,
        },
        "results": {
            symbol: {
                "status": "OK",
                "manifest": {
                    "cache_dir": str(root / symbol),
                    "requested_start": HOLDOUT_DOWNLOAD_START.isoformat(),
                    "requested_end": HOLDOUT_EVALUATION_END.isoformat(),
                },
            }
            for symbol in REQUIRED_SYMBOLS
        },
    }


def test_holdout_backfill_guard_accepts_exact_frozen_window(tmp_path: Path) -> None:
    report = validate_holdout_backfill(_summary(tmp_path), tmp_path)
    assert report["passed"] is True
    assert report["errors"] == []


def test_holdout_backfill_guard_rejects_discovery_overlap(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary["window"]["evaluation_end"] = "2026-07-11T23:59:59.999999+00:00"
    report = validate_holdout_backfill(summary, tmp_path)
    assert report["passed"] is False
    assert any("evaluation_end" in error or "overlaps discovery" in error for error in report["errors"])


def _schedule(expectancy: str, pf: str, trades: int, *, ci_low: float = -0.02) -> dict:
    return {
        "lifecycle_mean_reversion": {
            "trades_with_r": trades,
            "win_rate": "0.45",
            "expectancy_r": expectancy,
            "profit_factor": pf,
            "pnl_net": "500",
            "expectancy_ci": {"low": ci_low, "high": 0.30, "mean": float(expectancy)},
        },
        "groups": {"by_symbol": {}},
    }


def _full1h_symbols(*, concentrated: bool = False) -> dict:
    out = {}
    pnls = ["700", "250", "150", "-80", "-50"] if concentrated else ["260", "230", "210", "-70", "-40"]
    exps = ["0.20", "0.15", "0.10", "-0.03", "-0.02"]
    for symbol, pnl, exp in zip(REQUIRED_SYMBOLS, pnls, exps):
        out[symbol] = {
            "metrics": {
                "trades_with_r": 24,
                "expectancy_r": exp,
                "profit_factor": "1.30" if Decimal(exp) > 0 else "0.90",
                "pnl_net": pnl,
            }
        }
    return out


def test_assessment_returns_strong_pass_for_robust_frozen_case() -> None:
    results = {
        "CURRENT_DAYTIME_2H_07_21": _schedule("0.12", "1.30", 90, ci_low=0.01),
        "FULL_24H_2H": _schedule("0.10", "1.27", 95),
        "FULL_24H_1H": _schedule("0.09", "1.22", 120),
    }
    results["FULL_24H_1H"]["groups"]["by_symbol"] = _full1h_symbols()
    assessment = assess_holdout(results)
    assert assessment["classification"] == "HOLDOUT_STRONG_PASS"
    assert assessment["base_pass"] is True
    assert assessment["strong_pass"] is True


def test_assessment_fails_when_profit_factor_rule_is_not_met() -> None:
    results = {
        "CURRENT_DAYTIME_2H_07_21": _schedule("0.08", "1.10", 90),
        "FULL_24H_2H": _schedule("0.05", "1.08", 95),
        "FULL_24H_1H": _schedule("0.04", "1.06", 120),
    }
    results["FULL_24H_1H"]["groups"]["by_symbol"] = _full1h_symbols()
    assessment = assess_holdout(results)
    assert assessment["classification"] == "HOLDOUT_FAIL"
    assert assessment["checks"]["profit_factor_at_least_1_15_schedules"]["passed"] is False


def test_assessment_fails_single_symbol_concentration() -> None:
    results = {
        "CURRENT_DAYTIME_2H_07_21": _schedule("0.12", "1.30", 90),
        "FULL_24H_2H": _schedule("0.10", "1.27", 95),
        "FULL_24H_1H": _schedule("0.09", "1.22", 120),
    }
    results["FULL_24H_1H"]["groups"]["by_symbol"] = _full1h_symbols(concentrated=True)
    assessment = assess_holdout(results)
    assert assessment["classification"] == "HOLDOUT_FAIL"
    assert assessment["checks"]["full_1h_symbol_concentration"]["passed"] is False
