from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research" / "validate_extended_cache_no_lookahead.py"
SPEC = importlib.util.spec_from_file_location("validate_extended_cache_no_lookahead", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

UTC = dt.timezone.utc


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")


def test_validate_symbol_accepts_only_latest_closed_5m_price(tmp_path: Path) -> None:
    t10 = dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    t1005 = t10 + dt.timedelta(minutes=5)
    t1010 = t10 + dt.timedelta(minutes=10)
    _write(tmp_path / "mark_5m.json", [
        {"open_time": t10.isoformat(), "close": "100"},
        {"open_time": t1005.isoformat(), "close": "102"},
        {"open_time": t1010.isoformat(), "close": "999"},
    ])
    _write(tmp_path / "index_5m.json", [
        {"open_time": t10.isoformat(), "close": "99"},
        {"open_time": t1005.isoformat(), "close": "101"},
        {"open_time": t1010.isoformat(), "close": "998"},
    ])
    _write(tmp_path / "derivatives_reconstructed.json", [
        {"source_timestamp": t1005.isoformat(), "mark_price": "100", "index_price": "99"},
        {"source_timestamp": t1010.isoformat(), "mark_price": "102", "index_price": "101"},
    ])

    result = mod.validate_symbol(tmp_path)
    assert result["passed"] is True
    assert result["violations"] == 0
    assert result["derivative_rows"] == 2


def test_validate_symbol_fails_closed_on_empty_derivatives(tmp_path: Path) -> None:
    t10 = dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    _write(tmp_path / "mark_5m.json", [{"open_time": t10.isoformat(), "close": "100"}])
    _write(tmp_path / "index_5m.json", [{"open_time": t10.isoformat(), "close": "99"}])
    _write(tmp_path / "derivatives_reconstructed.json", [])

    with pytest.raises(ValueError, match="derivatives_reconstructed cache is empty"):
        mod.validate_symbol(tmp_path)


def test_main_fails_when_requested_symbol_is_absent_from_summary(tmp_path: Path) -> None:
    (tmp_path / "backfill_summary.json").write_text(
        json.dumps({"results": {}}), encoding="utf-8"
    )
    args = argparse.Namespace(cache_root=str(tmp_path), symbols="BTCUSDT")
    payload = mod.main(args)

    assert payload["passed"] is False
    assert payload["results"]["BTCUSDT"]["passed"] is False
    assert "absent" in payload["results"]["BTCUSDT"]["error"]
