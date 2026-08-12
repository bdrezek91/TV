from __future__ import annotations

import csv
import datetime as dt
import gzip
from decimal import Decimal

from tradingview_mcp.core.backtest.extended_history_v1 import (
    ExtendedHistoryManifest,
    aggregate_trade_archive_1m,
    parse_funding_rows,
    parse_kline_rows,
    parse_open_interest_rows,
    parse_price_kline_rows,
    parse_ratio_rows,
    public_trade_archive_urls,
    reconstruct_derivative_snapshots,
    rest_query_plan,
)


UTC = dt.timezone.utc


def test_trade_archive_urls_are_daily_and_use_official_path_shape():
    urls = public_trade_archive_urls(
        "btcusdt",
        dt.datetime(2026, 8, 8, 12, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 1, tzinfo=UTC),
    )
    assert urls == [
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-08.csv.gz",
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-09.csv.gz",
    ]


def test_rest_plan_uses_current_endpoint_limits_and_time_bounds():
    start = dt.datetime(2026, 7, 1, tzinfo=UTC)
    end = dt.datetime(2026, 8, 1, tzinfo=UTC)
    plan = {x["name"]: x for x in rest_query_plan("BTCUSDT", start, end)}
    assert plan["kline_1m"]["page_limit"] == 1000
    assert plan["open_interest_5m"]["page_limit"] == 200
    assert plan["funding"]["page_limit"] == 200
    assert plan["long_short_ratio_5m"]["page_limit"] == 500
    assert plan["open_interest_5m"]["params"]["intervalTime"] == "5min"
    assert plan["long_short_ratio_5m"]["params"]["period"] == "5min"


def test_kline_parser_sorts_oldest_first_and_uses_decimals():
    response = {"result": {"list": [
        ["2000", "2", "3", "1", "2.5", "10", "25"],
        ["1000", "1", "2", "0.5", "1.5", "5", "7.5"],
    ]}}
    rows = parse_kline_rows(response)
    assert [r["open_time"].timestamp() for r in rows] == [1.0, 2.0]
    assert rows[0]["close"] == Decimal("1.5")
    assert rows[1]["turnover"] == Decimal("25")


def test_price_kline_parser_accepts_mark_index_shape_without_volume():
    response = {"result": {"list": [
        ["2000", "102", "104", "101", "103"],
        ["1000", "100", "103", "99", "102"],
    ]}}
    rows = parse_price_kline_rows(response)
    assert [r["open_time"].timestamp() for r in rows] == [1.0, 2.0]
    assert rows[0] == {
        "open_time": dt.datetime.fromtimestamp(1, tz=UTC),
        "open": Decimal("100"),
        "high": Decimal("103"),
        "low": Decimal("99"),
        "close": Decimal("102"),
    }


def test_derivatives_parsers_normalize_timestamps_and_values():
    oi = parse_open_interest_rows({"result": {"list": [{"timestamp": "1000", "openInterest": "12.5"}]}})
    funding = parse_funding_rows({"result": {"list": [{"fundingRateTimestamp": "2000", "fundingRate": "0.0001"}]}})
    ratio = parse_ratio_rows({"result": {"list": [{"timestamp": "3000", "buyRatio": "0.6", "sellRatio": "0.4"}]}})
    assert oi[0]["open_interest"] == Decimal("12.5")
    assert funding[0]["funding_rate"] == Decimal("0.0001")
    assert ratio[0]["long_short_ratio"] == Decimal("1.5")


def test_derivative_reconstruction_uses_only_closed_price_klines_at_oi_timestamp():
    t10 = dt.datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    t1005 = dt.datetime(2026, 8, 10, 10, 5, tzinfo=UTC)
    t1010 = dt.datetime(2026, 8, 10, 10, 10, tzinfo=UTC)
    t1015 = dt.datetime(2026, 8, 10, 10, 15, tzinfo=UTC)

    oi = [
        {"source_timestamp": t1005, "open_interest": Decimal("10")},
        {"source_timestamp": t1010, "open_interest": Decimal("11")},
    ]
    mark = [
        {"open_time": t10, "close": Decimal("100")},
        {"open_time": t1005, "close": Decimal("102")},
        {"open_time": t1010, "close": Decimal("105")},  # closes at 10:15, future at 10:10
        {"open_time": t1015, "close": Decimal("999")},
    ]
    index = [
        {"open_time": t10, "close": Decimal("99")},
        {"open_time": t1005, "close": Decimal("101")},
        {"open_time": t1010, "close": Decimal("103")},  # closes at 10:15, future at 10:10
        {"open_time": t1015, "close": Decimal("1")},
    ]
    funding = [
        {"source_timestamp": t10, "funding_rate": Decimal("0.0001")},
        {"source_timestamp": t1015, "funding_rate": Decimal("0.9")},
    ]
    ratio = [
        {"source_timestamp": t10, "long_short_ratio": Decimal("1.2")},
        {"source_timestamp": t1015, "long_short_ratio": Decimal("9")},
    ]

    rows = reconstruct_derivative_snapshots(oi, mark, index, funding, ratio)
    assert len(rows) == 2

    # At 10:05 the 10:00-10:05 kline has just closed and is knowable.
    assert rows[0]["mark_price"] == Decimal("100")
    assert rows[0]["index_price"] == Decimal("99")
    assert rows[0]["funding_rate"] == Decimal("0.0001")
    assert rows[0]["long_short_ratio"] == Decimal("1.2")
    assert rows[0]["open_interest_value"] == Decimal("1000")
    assert rows[0]["basis"] == Decimal("1")

    # At 10:10 only the 10:05-10:10 mark/index candle is closed. The
    # 10:10-10:15 close must not leak five minutes backward into this OI row.
    assert rows[1]["mark_price"] == Decimal("102")
    assert rows[1]["index_price"] == Decimal("101")
    assert rows[1]["mark_price"] != Decimal("105")
    assert rows[1]["index_price"] != Decimal("103")
    assert rows[1]["funding_rate"] == Decimal("0.0001")
    assert rows[1]["long_short_ratio"] == Decimal("1.2")


def test_trade_archive_streams_into_same_one_minute_aggregation(tmp_path):
    path = tmp_path / "BTCUSDT2026-08-08.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["timestamp", "symbol", "side", "size", "price", "trdMatchID"])
        writer.writeheader()
        writer.writerow({"timestamp": "1786147200.10", "symbol": "BTCUSDT", "side": "Buy", "size": "1", "price": "60000", "trdMatchID": "a"})
        writer.writerow({"timestamp": "1786147201.20", "symbol": "BTCUSDT", "side": "Sell", "size": "0.5", "price": "60010", "trdMatchID": "b"})
    rows = aggregate_trade_archive_1m(path, "BTCUSDT")
    assert len(rows) == 1
    row = rows[0]
    assert row["buy_taker_volume"] == Decimal("1")
    assert row["sell_taker_volume"] == Decimal("0.5")
    assert row["delta"] == Decimal("0.5")
    # Only the 1 BTC x 60k trade exceeds the live $50k threshold.
    assert row["large_trade_count"] == 1


def test_manifest_fails_closed_for_unavailable_liquidations_and_orderbook():
    m = ExtendedHistoryManifest(
        "BTCUSDT",
        dt.datetime(2026, 7, 1, tzinfo=UTC),
        dt.datetime(2026, 8, 1, tzinfo=UTC),
    )
    m.note_source("klines", available=True, rows=1000)
    m.note_source("liquidations", available=False, detail="no validated historical source")
    m.note_source("orderbook", available=False, detail="download product exists; downloader not validated")
    payload = m.to_dict()
    assert payload["exact_full_source_replay"] is False
    assert "DEGRADED_NO_HISTORICAL_LIQUIDATIONS" in payload["degradations"]
    assert "DEGRADED_NO_HISTORICAL_ORDERBOOK" in payload["degradations"]
