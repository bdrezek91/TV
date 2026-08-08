"""Regression test for a bug found live on the VPS: `_default_analyzer`
called `run_multi_timeframe_analysis` with a bare symbol ("BTCUSDT")
instead of a fully-qualified "EXCHANGE:SYMBOL" string, which that
function's own docstring requires. Every timeframe failed against
TradingView's scanner as a result, surfacing only as a generic
"upstream cliff" abort in the collector logs -- `analyze_coin` (called
right after, with the bare symbol) worked fine, which is what made this
easy to miss: only the multi-timeframe half was broken.
"""
from __future__ import annotations

import pytest

from tradingview_mcp.core.analysis import tradingview_context as tvctx
from tradingview_mcp.core.services import screener_service


def test_default_analyzer_calls_multi_timeframe_with_fully_qualified_symbol(monkeypatch):
    captured = {}

    def fake_run_mtf(symbol, exchange):
        captured["mtf_symbol"] = symbol
        captured["mtf_exchange"] = exchange
        return {"alignment": "BULLISH"}

    def fake_analyze_coin(symbol, exchange, timeframe):
        captured["single_symbol"] = symbol
        captured["single_exchange"] = exchange
        return {"rsi": 55}

    monkeypatch.setattr(screener_service, "run_multi_timeframe_analysis", fake_run_mtf)
    monkeypatch.setattr(screener_service, "analyze_coin", fake_analyze_coin)

    result = tvctx._default_analyzer("BTCUSDT", "BYBIT")

    # run_multi_timeframe_analysis must receive a prefixed symbol
    # ("BYBIT:BTCUSDT"), matching exactly what the public
    # `multi_timeframe_analysis` MCP tool passes in server.py -- a bare
    # symbol here is the exact bug that was live on the VPS.
    assert captured["mtf_symbol"] == "BYBIT:BTCUSDT"
    assert ":" in captured["mtf_symbol"]

    # analyze_coin resolves bare symbol + exchange itself (matches
    # server.py's coin_analysis tool, which passes the bare symbol
    # straight through) -- must NOT be prefixed the same way.
    assert captured["single_symbol"] == "BTCUSDT"
    assert captured["single_exchange"] == "BYBIT"

    assert result == {"mtf": {"alignment": "BULLISH"}, "single": {"rsi": 55}}


def test_fetch_tradingview_context_default_parameter_is_the_real_analyzer():
    """`analyzer` is a default *parameter value*, bound at function-definition
    time — monkeypatching the `_default_analyzer` module attribute later
    would NOT affect it (Python captures the reference once). Confirm the
    binding is what worker code actually relies on when it calls
    `fetch_tradingview_context(symbol, "BYBIT")` with no analyzer override."""
    import inspect

    sig = inspect.signature(tvctx.fetch_tradingview_context)
    assert sig.parameters["analyzer"].default is tvctx._default_analyzer


@pytest.mark.asyncio
async def test_fetch_tradingview_context_wires_symbol_exchange_to_analyzer():
    calls = []

    def fake_analyzer(symbol, exchange):
        calls.append((symbol, exchange))
        return {"mtf": {}, "single": {}}

    ctx = await tvctx.fetch_tradingview_context("BTCUSDT", "BYBIT", analyzer=fake_analyzer)

    assert calls == [("BTCUSDT", "BYBIT")]
    assert ctx.symbol == "BTCUSDT"
    assert ctx.available is True
