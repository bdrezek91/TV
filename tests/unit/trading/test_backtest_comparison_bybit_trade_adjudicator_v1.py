from __future__ import annotations

import datetime as dt
import gzip
import importlib.util
import io
from decimal import Decimal
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research" / "adjudicate_bybit_trade_volume_mismatches.py"
SPEC = importlib.util.spec_from_file_location("adjudicate_bybit_trade_volume_mismatches", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

UTC = dt.timezone.utc


def _raw_csv(rows: list[tuple[str, str, str]]) -> bytes:
    text = "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional,RPI\n"
    for timestamp, side, size in rows:
        text += f"{timestamp},SOLUSDT,{side},{size},100,PlusTick,id,0,0,0,false\n"
    return gzip.compress(text.encode("utf-8"))


def test_raw_archive_aggregation_uses_exact_timestamp_side_and_size() -> None:
    minute = dt.datetime(2026, 7, 2, 3, 36, tzinfo=UTC)
    raw = _raw_csv([
        ("1782963360.2389", "Buy", "2259.8"),
        ("1782963418.2579", "Sell", "2301.4"),
        ("1782963420.0000", "Buy", "999"),  # next minute, excluded
    ])

    result = mod.aggregate_raw_minutes(raw, {minute})[minute]

    assert result["trade_count"] == 2
    assert result["buy"] == Decimal("2259.8")
    assert result["sell"] == Decimal("2301.4")
    assert result["total"] == Decimal("4561.2")


def test_source_disagreement_is_not_cache_error(monkeypatch, tmp_path: Path) -> None:
    minute = dt.datetime(2026, 7, 2, 3, 36, tzinfo=UTC)
    monkeypatch.setattr(
        mod,
        "find_mismatches",
        lambda cache_dir, rel_tol: [{
            "minute": minute,
            "kline_volume": Decimal("4562.3"),
            "cached_buy": Decimal("2259.8"),
            "cached_sell": Decimal("2301.4"),
            "cached_trade_volume": Decimal("4561.2"),
            "relative_error": Decimal("0.0002411064594612366569493457247"),
        }],
    )
    monkeypatch.setattr(
        mod,
        "fetch_raw_day",
        lambda client, symbol, day: _raw_csv([
            ("1782963360.2389", "Buy", "2259.8"),
            ("1782963418.2579", "Sell", "2301.4"),
        ]),
    )

    result = mod.adjudicate_symbol(object(), "SOLUSDT", tmp_path, rel_tol=Decimal("0.000001"))

    assert result["passed"] is True
    assert result["cache_errors"] == 0
    assert result["official_source_disagreements"] == 1
    row = result["adjudications"][0]
    assert row["classification"] == "OFFICIAL_SOURCE_DISAGREEMENT"
    assert row["raw_trade_volume"] == "4561.2"
    assert row["cached_trade_volume"] == "4561.2"
    assert row["kline_volume"] == "4562.3"


def test_raw_archive_difference_is_cache_error(monkeypatch, tmp_path: Path) -> None:
    minute = dt.datetime(2026, 7, 2, 3, 36, tzinfo=UTC)
    monkeypatch.setattr(
        mod,
        "find_mismatches",
        lambda cache_dir, rel_tol: [{
            "minute": minute,
            "kline_volume": Decimal("4562.3"),
            "cached_buy": Decimal("2259.8"),
            "cached_sell": Decimal("2301.4"),
            "cached_trade_volume": Decimal("4561.2"),
            "relative_error": Decimal("0.0002411064594612366569493457247"),
        }],
    )
    monkeypatch.setattr(
        mod,
        "fetch_raw_day",
        lambda client, symbol, day: _raw_csv([
            ("1782963360.2389", "Buy", "2259.8"),
            ("1782963418.2579", "Sell", "2302.5"),
        ]),
    )

    result = mod.adjudicate_symbol(object(), "SOLUSDT", tmp_path, rel_tol=Decimal("0.000001"))

    assert result["passed"] is False
    assert result["cache_errors"] == 1
    assert result["official_source_disagreements"] == 0
    assert result["adjudications"][0]["classification"] == "CACHE_ERROR"
