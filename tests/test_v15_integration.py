from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_v1 import TradeObservation, Variant

UTC = dt.timezone.utc
SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "research"
    / "backtest_research_v15_relative_value_development.py"
)
SPEC = importlib.util.spec_from_file_location(
    "backtest_research_v15_relative_value_development", SCRIPT
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _write_rows(path: Path) -> None:
    path.write_text(json.dumps({"rows": []}), encoding="utf-8")


def _synthetic_cache(tmp_path: Path) -> tuple[Path, dt.datetime, dt.datetime]:
    cache_root = tmp_path / "observed_development" / "cache"
    cache_root.mkdir(parents=True)
    start = dt.datetime(2026, 8, 10, tzinfo=UTC)
    raw_end = start + dt.timedelta(days=1, hours=12)
    results = {}
    for symbol in REQUIRED_SYMBOLS:
        cache_dir = cache_root / f"{symbol}_synthetic"
        cache_dir.mkdir()
        (cache_dir / "manifest.json").write_text(
            json.dumps({"symbol": symbol, "sources": {}}), encoding="utf-8"
        )
        for name in (
            "kline_1m.json",
            "kline_15m.json",
            "kline_1h.json",
            "kline_4h.json",
            "derivatives_reconstructed.json",
        ):
            _write_rows(cache_dir / name)
        results[symbol] = {"status": "OK", "manifest": {"cache_dir": str(cache_dir)}}
    (cache_root / "backfill_summary.json").write_text(
        json.dumps(
            {
                "window": {
                    "evaluation_start": start.isoformat(),
                    "evaluation_end": raw_end.isoformat(),
                },
                "results": results,
            }
        ),
        encoding="utf-8",
    )
    return cache_root, start, raw_end


def _fake_collect_pair(alt_symbol, _alt_row, _btc_bundle, cutoffs):
    strength = Decimal(str(runner.SYMBOL_ORDER[alt_symbol]))
    events = []
    for cutoff in cutoffs:
        observation = TradeObservation(
            variant=Variant.RESEARCH_V2,
            symbol=f"{alt_symbol}/BTCUSDT",
            decision_time=cutoff,
            setup_name="V15_SYNTHETIC",
            direction="SHORT_ALT_LONG_BTC",
            filled=True,
            r_multiple=Decimal("1"),
        )
        events.append(
            {
                "alt_symbol": alt_symbol,
                "pair": f"{alt_symbol}/BTCUSDT",
                "decision_time": cutoff,
                "pair_direction": observation.direction,
                "entry_z": strength,
                "beta": Decimal("1"),
                "exit_time": cutoff + dt.timedelta(minutes=30),
                "exit_reason": "RESIDUAL_REVERTED",
                "observation": observation,
            }
        )
    return events, Counter({"eligible_pair_signal": len(cutoffs)})


def test_v15_runner_selects_cross_section_before_outcomes_and_audits_cadence(
    tmp_path: Path, monkeypatch
) -> None:
    cache_root, start, raw_end = _synthetic_cache(tmp_path)
    output = tmp_path / "v15_result.json"
    monkeypatch.setattr(runner, "_collect_pair", _fake_collect_pair)

    payload = runner.main(
        argparse.Namespace(cache_root=str(cache_root), output=str(output))
    )

    assert payload["status"] == "POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION"
    assert payload["reserved_mr_v2_120d_holdout_used"] is False
    assert payload["evaluation_window"] == {
        "start": start,
        "raw_end": raw_end,
        "decision_end": raw_end - dt.timedelta(hours=24),
    }
    assert output.exists()

    one_hour = payload["results"]["FULL_24H_1H"]
    two_hour = payload["results"]["FULL_24H_2H"]
    assert one_hour["schedule"]["cutoffs"] == 13
    assert two_hour["schedule"]["cutoffs"] == 7
    assert one_hour["cross_section"]["weaker_same_scan_pairs_suppressed"] == 13 * 3
    assert set(one_hour["by_pair"]) == {"BNBUSDT/BTCUSDT"}

    overlap = payload["cadence_diagnostics"]
    assert overlap["full_1h_events"] == 13
    assert overlap["full_2h_events"] == 7
    assert overlap["common_events"] == 7
    assert overlap["only_full_2h"] == 0
    assert overlap["only_full_1h"] == 6
    assert overlap["common_r_mismatches"] == 0
    assert overlap["jaccard"] == 7 / 13
    assert payload["development_assessment"]["classification"] == "DEVELOPMENT_FAIL"
    assert (
        payload["development_assessment"]["checks"]["full_1h_terminal_pairs"]["passed"]
        is False
    )


def test_v15_development_pass_requires_every_frozen_gate() -> None:
    metrics = {
        "signals": 100,
        "filled": 100,
        "trades_with_r": 100,
        "expectancy_r": "0.20",
        "profit_factor": "1.50",
        "expectancy_ci": {"low": 0.05, "high": 0.35},
    }
    by_pair = {f"{symbol}/BTCUSDT": {"pnl_net": "100"} for symbol in runner.ALT_SYMBOLS}
    results = {
        name: {"lifecycle": dict(metrics), "by_pair": dict(by_pair)}
        for name in runner.SCHEDULES
    }

    assessment = runner.assess_v15_development(results, {"common_r_mismatches": 0})

    assert assessment["classification"] == "DEVELOPMENT_PASS"
    assert assessment["eligible_to_freeze_for_new_v15_holdout"] is True
    assert all(check["passed"] for check in assessment["checks"].values())
