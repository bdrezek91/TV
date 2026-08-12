"""Every MCP tool must carry directory-grade annotations.

Both official marketplaces (Anthropic's Connectors Directory and OpenAI's
ChatGPT plugin directory) require each tool to declare a human-readable
`title` and the applicable safety hint. Submissions with unannotated tools
are rejected at the Tools step of the portal, so this test makes the
requirement permanent: a newly added tool without annotations fails CI here
before it can fail a directory review.

Most tools in this server are read-only market analysis.  The explicit
exception is ``research_advance_paper_trading``: it advances and persists the
local paper portfolio, so advertising it as read-only would be inaccurate.
The allowlist below keeps that stateful surface small and reviewable.
"""
from tradingview_mcp.server import mcp


STATE_MUTATING_TOOLS = {"research_advance_paper_trading"}


def _all_tools():
    tools = mcp._tool_manager.list_tools()
    assert len(tools) >= 30, f"tool count suspiciously low: {len(tools)}"
    return tools


def test_every_tool_has_annotations_with_title():
    missing = [t.name for t in _all_tools() if t.annotations is None or not (t.annotations.title or "").strip()]
    assert not missing, f"tools missing annotations/title (directory submissions reject these): {missing}"


def test_tool_read_only_hints_match_state_contract():
    not_ro = {
        t.name
        for t in _all_tools()
        if t.annotations is None or t.annotations.readOnlyHint is not True
    }
    assert not_ro == STATE_MUTATING_TOOLS, (
        "readOnlyHint exceptions differ from the reviewed state-mutating "
        f"tool set: {sorted(not_ro)}"
    )


def test_every_tool_explicitly_declares_non_destructive():
    # OpenAI's plugin scanner requires an EXPLICIT true/false for
    # destructiveHint on every tool — omitting it fails the MCP scan step.
    missing = [t.name for t in _all_tools() if t.annotations is None or t.annotations.destructiveHint is not False]
    assert missing == [], f"tools without explicit destructiveHint=False: {missing}"


def test_titles_are_unique_and_human_readable():
    tools = _all_tools()
    titles = [t.annotations.title for t in tools]
    assert len(set(titles)) == len(titles), "duplicate tool titles confuse directory listings"
    for title in titles:
        assert title != title.lower(), f"title looks like an identifier, not a human title: {title!r}"
        assert "_" not in title, f"title contains underscores: {title!r}"
