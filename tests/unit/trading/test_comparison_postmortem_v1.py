from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_postmortem_v1 import (
    POSTMORTEM_SCHEMA_VERSION,
    apply_lifecycle_gate,
    build_event_record,
    flatten_event,
    jsonable,
)
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, TradeObservation, Variant

UTC = dt.timezone.utc


def _chain() -> dict:
    return {
        "regime": {"primary_regime": "RANGE", "confidence": 0.61},
        "setups": {
            "mean_reversion": {
                "setup_type": "mean_reversion",
                "direction": "LONG",
                "entry_zone": {"low": Decimal("99"), "high": Decimal("100")},
                "stop_loss": Decimal("98"),
                "targets": [Decimal("102"), Decimal("105")],
                "confidence": 0.55,
                "orderflow_confirmed": True,
                "price_only_mode": False,
            }
        },
        "confirmations": {"mean_reversion": {"status": "WEAK_CONFIRMATION", "confidence": 0.52}},
        "risk_decisions": {"mean_reversion": {"decision": "APPROVED"}},
        "data_quality": {"data_quality_score": 96},
        "w1_classification_data_quality": {"data_quality_score": 100},
        "features": {
            "tf": {
                "1h": {
                    "last_close": Decimal("99.5"),
                    "atr": Decimal("1.0"),
                    "support": Decimal("99"),
                    "resistance": Decimal("105"),
                    "position_in_range": Decimal("0.0833"),
                    "ema20": Decimal("101"),
                    "ema50": Decimal("102"),
                },
                "15m": {"last_close": Decimal("99.4"), "atr": Decimal("0.4")},
            },
            "volume": {"cvd_slope_15m": Decimal("2.5"), "volume_zscore": Decimal("1.2")},
            "futures": {"oi_change_1h_pct": Decimal("0.8"), "funding_rate": Decimal("0.0001")},
            "liquidations": {},
            "orderbook": {},
        },
        "_windows": object(),
        "_state": {"hidden": True},
    }


def _event(at: dt.datetime, terminal_at: dt.datetime) -> dict:
    signal = NeutralSignal(
        variant=Variant.RESEARCH_V2,
        symbol="BTCUSDT",
        decision_time=at,
        setup_name="mean_reversion",
        direction="LONG",
        entry_low=Decimal("99"),
        entry_high=Decimal("100"),
        stop_loss=Decimal("98"),
        targets=(Decimal("102"), Decimal("105")),
        regime="RANGE",
        score=Decimal("0.55"),
        metadata={"confirmation_status": "WEAK_CONFIRMATION"},
    )
    observation = TradeObservation(
        variant=Variant.RESEARCH_V2,
        symbol="BTCUSDT",
        decision_time=at,
        setup_name="mean_reversion",
        direction="LONG",
        filled=True,
        r_multiple=Decimal("0.25"),
        pnl_net=Decimal("25"),
        fees=Decimal("5"),
        slippage=Decimal("2"),
        funding=Decimal("1"),
    )
    order = {
        "status": "CLOSED",
        "entry_time": (at + dt.timedelta(minutes=5)).isoformat(),
        "avg_entry_price": Decimal("99.5"),
        "filled_size": Decimal("1"),
        "remaining_size": Decimal("0"),
        "state_history": [
            {"status": "CLOSED", "at": terminal_at.isoformat(), "detail": "test close"}
        ],
        "exits": [],
    }
    result = SimpleNamespace(
        observation=observation,
        order=order,
        risk_amount=Decimal("100"),
        production_pnl_as_is=Decimal("30"),
        corrected_net_pnl=Decimal("25"),
        r_multiple_production=Decimal("0.30"),
        r_multiple_corrected=Decimal("0.25"),
        entry_fee_omitted_by_production_pnl=Decimal("5"),
        target_schedule_policy="NORMALIZE_AVAILABLE_TARGETS",
        intrabar_policy="NEXT_CANDLE",
    )
    return build_event_record(schedule="FULL_24H_1H", signal=signal, result=result, chain=_chain())


def test_event_record_excludes_large_private_replay_windows_and_flattens_features() -> None:
    at = dt.datetime(2026, 5, 1, 10, tzinfo=UTC)
    event = _event(at, at + dt.timedelta(hours=1))
    assert event["schema_version"] == POSTMORTEM_SCHEMA_VERSION
    assert "_windows" not in event["context"]
    assert "_state" not in event["context"]

    row = flatten_event(event)
    assert row["position_in_range_1h"] == "0.0833"
    assert row["cvd_slope_15m"] == "2.5"
    assert row["oi_change_1h_pct"] == "0.8"
    assert row["target_2"] == "105"


def test_lifecycle_annotation_matches_single_symbol_busy_semantics() -> None:
    at = dt.datetime(2026, 5, 1, 10, tzinfo=UTC)
    first = _event(at, at + dt.timedelta(hours=2))
    second = _event(at + dt.timedelta(hours=1), at + dt.timedelta(hours=3))
    third = _event(at + dt.timedelta(hours=2), at + dt.timedelta(hours=3))

    annotated = apply_lifecycle_gate([first, second, third], decision_end=at + dt.timedelta(days=1))
    assert [e["lifecycle"]["status"] for e in annotated] == [
        "SUBMITTED",
        "SUPPRESSED_BUSY",
        "SUBMITTED",
    ]


def test_jsonable_preserves_decimal_precision_and_warsaw_timestamp() -> None:
    at = dt.datetime(2026, 5, 1, 10, tzinfo=UTC)
    event = _event(at, at + dt.timedelta(hours=1))
    payload = jsonable(event)
    assert payload["observation"]["r_multiple"] == "0.25"
    assert payload["decision_time"] == "2026-05-01T10:00:00+00:00"
    assert payload["decision_time_warsaw"].endswith("+02:00")
