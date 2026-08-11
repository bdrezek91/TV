"""Frozen out-of-sample contract for Mean Reversion validation.

This module deliberately contains only pre-registered holdout constants,
backfill-window guards and PASS/FAIL assessment logic.  It does not generate
signals and does not alter Research V2, W1-W5 or production state.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

UTC = dt.timezone.utc

HOLDOUT_EVALUATION_START = dt.datetime(2026, 4, 12, 0, 0, tzinfo=UTC)
HOLDOUT_EVALUATION_END = dt.datetime(2026, 7, 10, 23, 59, 59, 999999, tzinfo=UTC)
HOLDOUT_WARMUP_DAYS = 10
HOLDOUT_DOWNLOAD_START = HOLDOUT_EVALUATION_START - dt.timedelta(days=HOLDOUT_WARMUP_DAYS)
DISCOVERY_START = dt.datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
MIN_FUTURE_EXECUTION = dt.timedelta(hours=12)
HOLDOUT_DECISION_END = HOLDOUT_EVALUATION_END - MIN_FUTURE_EXECUTION

REQUIRED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT")

# Git blob ids of the frozen discovery implementation at pre-registration.
# They are provenance markers in the report; the holdout runner reuses these
# modules without changing their logic.
FROZEN_SOURCE_BLOBS = {
    "regime_classifier_v2.py": "b7917da5637f8c28936ef1f9d6dd7ae3ed3b60ad",
    "setup_detector_v2.py": "bd63a82368bf394dd245e4a4f419f04e25bb4aa1",
    "orderflow_confirmation_v2.py": "d9eeca51310f811ccf4e5995833b26332f630300",
    "risk_manager_v2.py": "6812a76e374658eb114f9af89ba0fff66edeecb7",
    "comparison_candidate_v1.py": "82fe37fe0731cf8cdef567f70b23ec6471e055ca",
    "comparison_execution_v1.py": "5f174f91ec83ba33cec19a407fbe9ee9846861be",
}

PASS_RULE = {
    "positive_expectancy_schedules_min": 2,
    "profit_factor_threshold": Decimal("1.15"),
    "profit_factor_schedules_min": 2,
    "catastrophic_schedule_floor_expectancy_r": Decimal("-0.10"),
    "full_24h_1h_min_terminal_trades": 100,
    "full_24h_1h_positive_symbols_min": 3,
    "full_24h_1h_max_single_symbol_share_of_positive_pnl": Decimal("0.60"),
    "strong_profit_factor_threshold": Decimal("1.25"),
    "strong_profit_factor_schedules_min": 2,
    "strong_requires_positive_ci_low_schedules_min": 1,
}


def _dt(value: Any) -> dt.datetime:
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return out.astimezone(UTC)


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def validate_holdout_backfill(summary: Mapping[str, Any], cache_root: Path) -> dict[str, Any]:
    """Fail closed unless the cache is exactly the pre-registered holdout."""
    window = dict(summary.get("window") or {})
    actual_start = _dt(window.get("evaluation_start"))
    actual_end = _dt(window.get("evaluation_end"))
    actual_download_start = _dt(window.get("download_start"))
    actual_download_end = _dt(window.get("download_end"))

    errors: list[str] = []
    if actual_start != HOLDOUT_EVALUATION_START:
        errors.append(f"evaluation_start {actual_start.isoformat()} != frozen {HOLDOUT_EVALUATION_START.isoformat()}")
    if actual_end != HOLDOUT_EVALUATION_END:
        errors.append(f"evaluation_end {actual_end.isoformat()} != frozen {HOLDOUT_EVALUATION_END.isoformat()}")
    if actual_download_start != HOLDOUT_DOWNLOAD_START:
        errors.append(f"download_start {actual_download_start.isoformat()} != frozen {HOLDOUT_DOWNLOAD_START.isoformat()}")
    if actual_download_end != HOLDOUT_EVALUATION_END:
        errors.append("download_end does not equal frozen evaluation_end")
    if int(window.get("evaluation_days") or 0) != 90:
        errors.append("evaluation_days must equal 90")
    if int(window.get("warmup_days") or 0) != HOLDOUT_WARMUP_DAYS:
        errors.append(f"warmup_days must equal {HOLDOUT_WARMUP_DAYS}")
    if actual_end >= DISCOVERY_START:
        errors.append("holdout overlaps discovery window")

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
            errors.append(f"{symbol} cache_dir escapes dedicated holdout cache root: {cache_dir}")
        if manifest.get("requested_start") and _dt(manifest["requested_start"]) != HOLDOUT_DOWNLOAD_START:
            errors.append(f"{symbol} manifest requested_start differs from frozen download start")
        if manifest.get("requested_end") and _dt(manifest["requested_end"]) != HOLDOUT_EVALUATION_END:
            errors.append(f"{symbol} manifest requested_end differs from frozen download end")

    return {
        "passed": not errors,
        "errors": errors,
        "evaluation_start": HOLDOUT_EVALUATION_START,
        "evaluation_end": HOLDOUT_EVALUATION_END,
        "decision_end": HOLDOUT_DECISION_END,
        "discovery_start": DISCOVERY_START,
        "symbols": list(REQUIRED_SYMBOLS),
    }


def _schedule_metrics(results: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    row = results.get(name) or {}
    metrics = row.get("lifecycle_mean_reversion") or {}
    if not metrics:
        raise ValueError(f"missing lifecycle_mean_reversion for {name}")
    return metrics


def assess_holdout(results: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the pre-registered robustness rule without outcome tuning."""
    schedule_names = ("CURRENT_DAYTIME_2H_07_21", "FULL_24H_2H", "FULL_24H_1H")
    metrics = {name: _schedule_metrics(results, name) for name in schedule_names}

    expectancy = {name: _d(row.get("expectancy_r")) for name, row in metrics.items()}
    profit_factor = {name: _d(row.get("profit_factor")) for name, row in metrics.items()}
    if any(v is None for v in expectancy.values()):
        raise ValueError("all schedules require expectancy_r")

    positive_expectancy_count = sum(v > 0 for v in expectancy.values() if v is not None)
    pf_115_count = sum(v is not None and v >= PASS_RULE["profit_factor_threshold"] for v in profit_factor.values())
    floor_pass = all(v is not None and v >= PASS_RULE["catastrophic_schedule_floor_expectancy_r"] for v in expectancy.values())

    full1h = results.get("FULL_24H_1H") or {}
    full1h_metrics = metrics["FULL_24H_1H"]
    terminal_trades = int(full1h_metrics.get("trades_with_r") or 0)
    by_symbol = ((full1h.get("groups") or {}).get("by_symbol") or {})
    if set(by_symbol) != set(REQUIRED_SYMBOLS):
        raise ValueError("FULL_24H_1H by_symbol must contain exactly the frozen five-symbol universe")

    symbol_rows: dict[str, dict[str, Any]] = {}
    positive_pnls: list[Decimal] = []
    positive_symbols = 0
    for symbol in REQUIRED_SYMBOLS:
        sm = (by_symbol[symbol].get("metrics") or {})
        exp = _d(sm.get("expectancy_r"))
        pnl = _d(sm.get("pnl_net")) or Decimal("0")
        if exp is not None and exp > 0:
            positive_symbols += 1
        if pnl > 0:
            positive_pnls.append(pnl)
        symbol_rows[symbol] = {
            "trades_with_r": int(sm.get("trades_with_r") or 0),
            "expectancy_r": exp,
            "profit_factor": _d(sm.get("profit_factor")),
            "pnl_net": pnl,
        }

    gross_positive_pnl = sum(positive_pnls, Decimal("0"))
    max_positive_share = (
        max(positive_pnls) / gross_positive_pnl
        if positive_pnls and gross_positive_pnl > 0
        else Decimal("1")
    )

    checks = {
        "positive_expectancy_schedules": {
            "actual": positive_expectancy_count,
            "required": PASS_RULE["positive_expectancy_schedules_min"],
            "passed": positive_expectancy_count >= PASS_RULE["positive_expectancy_schedules_min"],
        },
        "profit_factor_at_least_1_15_schedules": {
            "actual": pf_115_count,
            "required": PASS_RULE["profit_factor_schedules_min"],
            "passed": pf_115_count >= PASS_RULE["profit_factor_schedules_min"],
        },
        "catastrophic_floor": {
            "floor": PASS_RULE["catastrophic_schedule_floor_expectancy_r"],
            "passed": floor_pass,
        },
        "full_1h_terminal_sample": {
            "actual": terminal_trades,
            "required": PASS_RULE["full_24h_1h_min_terminal_trades"],
            "passed": terminal_trades >= PASS_RULE["full_24h_1h_min_terminal_trades"],
        },
        "full_1h_positive_symbols": {
            "actual": positive_symbols,
            "required": PASS_RULE["full_24h_1h_positive_symbols_min"],
            "passed": positive_symbols >= PASS_RULE["full_24h_1h_positive_symbols_min"],
        },
        "full_1h_symbol_concentration": {
            "max_single_symbol_share_of_positive_pnl": max_positive_share,
            "maximum_allowed": PASS_RULE["full_24h_1h_max_single_symbol_share_of_positive_pnl"],
            "passed": max_positive_share <= PASS_RULE["full_24h_1h_max_single_symbol_share_of_positive_pnl"],
        },
    }

    base_pass = all(check["passed"] for check in checks.values())
    strong_pf_count = sum(v is not None and v >= PASS_RULE["strong_profit_factor_threshold"] for v in profit_factor.values())
    positive_ci_low_count = 0
    for row in metrics.values():
        ci_low = _d((row.get("expectancy_ci") or {}).get("low"))
        if ci_low is not None and ci_low > 0:
            positive_ci_low_count += 1
    strong_pass = (
        base_pass
        and strong_pf_count >= PASS_RULE["strong_profit_factor_schedules_min"]
        and positive_ci_low_count >= PASS_RULE["strong_requires_positive_ci_low_schedules_min"]
    )

    classification = "HOLDOUT_STRONG_PASS" if strong_pass else ("HOLDOUT_PASS" if base_pass else "HOLDOUT_FAIL")
    return {
        "classification": classification,
        "base_pass": base_pass,
        "strong_pass": strong_pass,
        "checks": checks,
        "schedule_metrics": {
            name: {
                "trades_with_r": int(row.get("trades_with_r") or 0),
                "win_rate": _d(row.get("win_rate")),
                "expectancy_r": expectancy[name],
                "profit_factor": profit_factor[name],
                "pnl_net": _d(row.get("pnl_net")),
                "expectancy_ci": row.get("expectancy_ci"),
            }
            for name, row in metrics.items()
        },
        "full_1h_by_symbol": symbol_rows,
        "strong_diagnostics": {
            "profit_factor_at_least_1_25_schedules": strong_pf_count,
            "positive_95pct_ci_low_schedules": positive_ci_low_count,
        },
        "interpretation": (
            "PASS means the frozen Mean Reversion specification survived this untouched holdout under the pre-registered robustness rule; "
            "it is not permission to retune on holdout. FAIL must not be rescued by changing symbols, hours, W3 status or thresholds on this same period."
        ),
    }
