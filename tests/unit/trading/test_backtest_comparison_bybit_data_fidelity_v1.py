from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from decimal import Decimal
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research" / "validate_bybit_data_fidelity.py"
SPEC = importlib.util.spec_from_file_location("validate_bybit_data_fidelity", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

UTC = dt.timezone.utc


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")


def _candle(ts: dt.datetime, *, volume: str = "3") -> dict:
    return {
        "open_time": ts.isoformat(),
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": volume,
        "turnover": "303",
    }


def test_candle_grid_passes_when_complete_and_fails_when_one_minute_missing() -> None:
    start = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    end = start + dt.timedelta(minutes=2, seconds=59)
    rows = [_candle(start + dt.timedelta(minutes=i)) for i in range(3)]

    ok = mod.validate_candle_dataset(rows, start=start, end=end, step_seconds=60, require_volume=True)
    assert ok["passed"] is True
    assert ok["expected_rows"] == 3

    bad = mod.validate_candle_dataset(rows[:1] + rows[2:], start=start, end=end, step_seconds=60, require_volume=True)
    assert bad["passed"] is False
    assert bad["missing"] == 1


def test_ratio_checks_sum_and_derived_ratio() -> None:
    start = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    row = {
        "source_timestamp": start.isoformat(),
        "buy_ratio": "0.49",
        "sell_ratio": "0.51",
        "long_short_ratio": str(Decimal("0.49") / Decimal("0.51")),
    }
    ok = mod.validate_5m_point_dataset(
        [row], start=start, end=start, kind="ratio", required_coverage=Decimal("1")
    )
    assert ok["passed"] is True

    bad_row = dict(row, buy_ratio="0.80", sell_ratio="0.40", long_short_ratio="2")
    bad = mod.validate_5m_point_dataset(
        [bad_row], start=start, end=start, kind="ratio", required_coverage=Decimal("1")
    )
    assert bad["passed"] is False
    assert bad["value_failures"] == 1


def test_public_trade_volume_is_cross_checked_against_independent_1m_kline(tmp_path: Path) -> None:
    start = dt.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    end = start + dt.timedelta(minutes=1, seconds=59)
    _write_rows(tmp_path / "kline_1m.json", [_candle(start, volume="3"), _candle(start + dt.timedelta(minutes=1), volume="7")])
    daily = tmp_path / "trades_1m_daily"
    daily.mkdir()
    (daily / "2026-08-01.json").write_text(
        json.dumps({
            "source_url": "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-01.csv.gz",
            "rows": [
                {"bucket_start": start.isoformat(), "buy_taker_volume": "1", "sell_taker_volume": "2", "delta": "-1"},
                {"bucket_start": (start + dt.timedelta(minutes=1)).isoformat(), "buy_taker_volume": "4", "sell_taker_volume": "3", "delta": "1"},
            ],
        }),
        encoding="utf-8",
    )

    ok = mod.validate_trade_volume_against_kline(
        tmp_path, start=start, end=end, rel_tolerance=Decimal("0.000001")
    )
    assert ok["passed"] is True
    assert ok["mismatches"] == 0

    payload = json.loads((daily / "2026-08-01.json").read_text(encoding="utf-8"))
    payload["rows"][1]["buy_taker_volume"] = "5"
    (daily / "2026-08-01.json").write_text(json.dumps(payload), encoding="utf-8")
    bad = mod.validate_trade_volume_against_kline(
        tmp_path, start=start, end=end, rel_tolerance=Decimal("0.000001")
    )
    assert bad["passed"] is False
    assert bad["mismatches"] == 1


def test_raw_response_comparison_does_not_reuse_research_parser() -> None:
    cache = {
        "open_time": "2026-08-01T00:00:00+00:00",
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": "3",
        "turnover": "303",
    }
    raw = ["1785542400000", "100", "102", "99", "101", "3", "303"]
    assert mod._compare_raw(cache, raw, kind="candle") == []

    raw_bad = list(raw)
    raw_bad[5] = "4"
    assert mod._compare_raw(cache, raw_bad, kind="candle") == ["volume"]


def test_sample_indices_are_deterministic_and_cover_edges() -> None:
    assert mod._sample_indices(10, 1) == [5]
    assert mod._sample_indices(10, 3) == [0, 4, 9]
