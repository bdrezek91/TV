import datetime as dt

from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import (
    SingleSymbolLifecycleGate,
    order_terminal_time,
)

UTC = dt.timezone.utc


def _t(hour):
    return dt.datetime(2026, 8, 1, hour, 0, tzinfo=UTC)


def test_terminal_time_uses_last_terminal_transition():
    order = {
        "state_history": [
            {"status": "PENDING_ENTRY", "at": _t(1).isoformat(), "detail": "x"},
            {"status": "OPEN", "at": _t(2).isoformat(), "detail": "x"},
            {"status": "STOPPED_OUT", "at": _t(5).isoformat(), "detail": "x"},
        ]
    }
    assert order_terminal_time(order, fallback_end=_t(10)) == _t(5)


def test_nonterminal_order_occupies_through_fallback_end():
    order = {
        "state_history": [
            {"status": "PENDING_ENTRY", "at": _t(1).isoformat(), "detail": "x"},
            {"status": "OPEN", "at": _t(2).isoformat(), "detail": "x"},
        ]
    }
    assert order_terminal_time(order, fallback_end=_t(10)) == _t(10)


def test_gate_suppresses_same_symbol_until_terminal_time():
    gate = SingleSymbolLifecycleGate()
    order = {
        "state_history": [
            {"status": "PENDING_ENTRY", "at": _t(1).isoformat(), "detail": "x"},
            {"status": "EXPIRED", "at": _t(4).isoformat(), "detail": "x"},
        ]
    }
    assert gate.can_submit("BTCUSDT", _t(1))
    gate.occupy_from_order("BTCUSDT", order, fallback_end=_t(10))
    assert not gate.can_submit("BTCUSDT", _t(3))
    assert gate.can_submit("BTCUSDT", _t(4))
    assert gate.diagnostics()["suppressed_signals_total"] == 1


def test_gate_is_independent_per_symbol():
    gate = SingleSymbolLifecycleGate()
    order = {"state_history": [{"status": "CLOSED", "at": _t(6).isoformat(), "detail": "x"}]}
    gate.occupy_from_order("BTCUSDT", order, fallback_end=_t(10))
    assert not gate.can_submit("BTCUSDT", _t(5))
    assert gate.can_submit("ETHUSDT", _t(5))
