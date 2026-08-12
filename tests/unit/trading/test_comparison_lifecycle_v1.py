import datetime as dt

from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import (
    SingleSymbolLifecycleGate,
    order_effective_terminal_time,
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


def _dust_order(*, remaining: str) -> dict:
    return {
        "filled_size": "10",
        "remaining_size": remaining,
        "targets": ["101", "102"],
        "tp_hit_flags": [True, True, False],
        "state_history": [
            {"status": "OPEN", "at": _t(1).isoformat(), "detail": "x"},
            {"status": "TP1_HIT", "at": _t(2).isoformat(), "detail": "x"},
            {"status": "TP2_HIT", "at": _t(3).isoformat(), "detail": "x"},
            {"status": "STOPPED_OUT", "at": _t(8).isoformat(), "detail": "dust later hit SL"},
        ],
    }


def test_dust_repair_is_opt_in_and_v1_default_stays_unchanged():
    order = _dust_order(remaining="5e-40")
    assert order_terminal_time(order, fallback_end=_t(10)) == _t(8)
    assert order_effective_terminal_time(order, fallback_end=_t(10)) == _t(8)
    assert order_effective_terminal_time(
        order,
        fallback_end=_t(10),
        close_target_dust=True,
    ) == _t(3)


def test_dust_aware_gate_releases_symbol_at_last_target():
    gate = SingleSymbolLifecycleGate(close_target_dust=True)
    gate.occupy_from_order("ETHUSDT", _dust_order(remaining="5e-40"), fallback_end=_t(10))
    assert not gate.can_submit("ETHUSDT", _t(2))
    assert gate.can_submit("ETHUSDT", _t(3))
    assert gate.diagnostics()["close_target_dust"] is True


def test_real_residual_position_is_not_reclassified_as_dust():
    order = _dust_order(remaining="2")  # 20% of the original fill remains.
    assert order_effective_terminal_time(
        order,
        fallback_end=_t(10),
        close_target_dust=True,
    ) == _t(8)
