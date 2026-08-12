from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, TradeObservation, Variant


UTC = dt.timezone.utc
SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "research"
    / "backtest_research_v14_breakout_development.py"
)
SPEC = importlib.util.spec_from_file_location("backtest_research_v14_breakout_development", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")


def _synthetic_cache(tmp_path: Path) -> tuple[Path, dt.datetime, dt.datetime]:
    cache_root = tmp_path / "revealed_development" / "cache"
    cache_root.mkdir(parents=True)
    start = dt.datetime(2026, 8, 10, tzinfo=UTC)
    raw_end = start + dt.timedelta(days=1)
    results = {}

    for symbol in REQUIRED_SYMBOLS:
        cache_dir = cache_root / f"{symbol}_synthetic"
        cache_dir.mkdir()
        (cache_dir / "manifest.json").write_text(
            json.dumps({"symbol": symbol, "sources": {}}), encoding="utf-8"
        )
        candles = [
            {
                "open_time": (start + dt.timedelta(hours=hour)).isoformat(),
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1",
            }
            for hour in range(25)
        ]
        _write_rows(cache_dir / "kline_1m.json", candles)
        for name in ("kline_15m.json", "kline_1h.json", "kline_4h.json", "derivatives_reconstructed.json"):
            _write_rows(cache_dir / name, [])
        results[symbol] = {
            "status": "OK",
            "manifest": {"cache_dir": str(cache_dir)},
        }

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


def _fake_chain(symbol, _index, cutoff, *, btc_regime, previous_state):
    step = 1 + int((previous_state or {}).get("step", 0))
    return {
        "symbol": symbol,
        "as_of": cutoff,
        "regime": {"primary_regime": "TREND_UP"},
        "_state": {"step": step},
        "btc_regime": btc_regime,
    }


def _fake_evaluate(chain, *, btc_regime):
    signal = NeutralSignal(
        variant=Variant.RESEARCH_V2,
        symbol=chain["symbol"],
        decision_time=chain["as_of"],
        setup_name="V14_SYNTHETIC_BREAKOUT",
        direction="LONG",
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("99"),
        targets=(Decimal("103"),),
        regime="TREND_UP",
        metadata={"btc_regime": btc_regime},
    )
    return {
        "setup": {"direction": "LONG"},
        "gates": {"eligible": True, "rejection_reasons": []},
        "signal": signal,
    }


def _fake_simulate(signal, future_candles):
    assert all(candle["open_time"] >= signal.decision_time for candle in future_candles)
    observation = TradeObservation(
        variant=signal.variant,
        symbol=signal.symbol,
        decision_time=signal.decision_time,
        setup_name=signal.setup_name,
        direction=signal.direction,
        filled=True,
        r_multiple=Decimal("1"),
    )
    order = {
        "state_history": [
            {"status": "CLOSED", "at": signal.decision_time + dt.timedelta(minutes=1)}
        ]
    }
    return SimpleNamespace(observation=observation, order=order)


def test_v14_runner_uses_real_cadences_and_reports_deterministic_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root, start, raw_end = _synthetic_cache(tmp_path)
    output = tmp_path / "v14_result.json"
    monkeypatch.setattr(runner, "build_research_v2_candidate_chain_indexed", _fake_chain)
    monkeypatch.setattr(runner, "evaluate_breakout_v14", _fake_evaluate)
    monkeypatch.setattr(runner, "simulate_neutral_signal", _fake_simulate)

    payload = runner.main(argparse.Namespace(cache_root=str(cache_root), output=str(output)))

    assert payload["status"] == "POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION"
    assert payload["evaluation_window"] == {
        "start": start,
        "raw_end": raw_end,
        "decision_end": raw_end - dt.timedelta(hours=12),
    }
    assert output.exists()

    one_hour = payload["results"]["FULL_24H_1H"]
    two_hour = payload["results"]["FULL_24H_2H"]
    assert one_hour["schedule"]["cutoffs_per_symbol"] == 13
    assert two_hour["schedule"]["cutoffs_per_symbol"] == 7

    for symbol in REQUIRED_SYMBOLS:
        one_times = [
            event["decision_time"] for event in one_hour["event_ledger"] if event["symbol"] == symbol
        ]
        two_times = [
            event["decision_time"] for event in two_hour["event_ledger"] if event["symbol"] == symbol
        ]
        assert all(right - left == dt.timedelta(hours=1) for left, right in zip(one_times, one_times[1:]))
        assert all(right - left == dt.timedelta(hours=2) for left, right in zip(two_times, two_times[1:]))
        assert set(two_times) <= set(one_times)

    overlap = payload["cadence_diagnostics"]
    assert overlap["full_1h_events"] == 13 * len(REQUIRED_SYMBOLS)
    assert overlap["full_2h_events"] == 7 * len(REQUIRED_SYMBOLS)
    assert overlap["common_events"] == overlap["full_2h_events"]
    assert overlap["only_full_2h"] == 0
    assert overlap["only_full_1h"] == 6 * len(REQUIRED_SYMBOLS)
    assert overlap["common_r_mismatches"] == 0
    assert overlap["jaccard"] == pytest.approx(7 / 13)
