"""Tests for the Block 3 MCP tool handlers in `server.py`.

These exercise the thin `server.py` layer itself (parameter validation +
delegation), not the underlying service logic (already covered by
`test_trading_query_service.py`). `@mcp.tool()` returns the original
function unmodified in this MCP SDK version, so handlers are directly
callable in-process — no client/transport needed.
"""
from __future__ import annotations


import pytest

import tradingview_mcp.server as server


@pytest.mark.asyncio
async def test_all_seven_block3_tools_are_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "bybit_market_state", "rank_futures_opportunities", "get_trade_setup",
        "paper_portfolio_status", "strategy_performance", "trading_system_health",
        "run_futures_opportunity_scan",
    }
    assert expected.issubset(names)


@pytest.mark.asyncio
async def test_bybit_market_state_validates_empty_symbol():
    result = await server.bybit_market_state("")
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_rank_futures_opportunities_validates_empty_symbols():
    result = await server.rank_futures_opportunities("")
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_rank_futures_opportunities_validates_score_range():
    result = await server.rank_futures_opportunities("BTCUSDT", minimum_score=150)
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_rank_futures_opportunities_validates_maximum_results():
    result = await server.rank_futures_opportunities("BTCUSDT", maximum_results=0)
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_get_trade_setup_validates_empty_signal_id():
    result = await server.get_trade_setup("")
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_strategy_performance_validates_days():
    result = await server.strategy_performance(days=0)
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_run_futures_opportunity_scan_validates_score_range():
    result = await server.run_futures_opportunity_scan(minimum_score=-1)
    assert result["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_existing_tool_count_did_not_shrink():
    """Regression guard: Block 3 must only ADD tools, never remove/replace
    an existing one. 37 tools existed before Block 3 (per the Block 1/2
    audit); Block 3 adds exactly 7."""
    tools = await server.mcp.list_tools()
    assert len(tools) >= 44


@pytest.mark.asyncio
async def test_a_sample_of_pre_existing_tools_still_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    # A representative sample from every pre-existing tool category —
    # proves Block 3 didn't accidentally rename/remove/shadow anything.
    for name in (
        "top_gainers", "top_losers", "coin_analysis", "multi_timeframe_analysis",
        "bollinger_scan", "backtest_strategy", "market_snapshot", "bitcoin_market_pulse",
        "futures_market_overview", "stock_screener", "financial_news",
    ):
        assert name in names, f"pre-existing tool {name!r} is missing after Block 3"
