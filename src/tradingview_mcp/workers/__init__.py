"""Standalone worker entry points (run as separate Docker Compose services).

None of these import `server.py` or talk to the MCP server over HTTP —
they only share the `core/` package (config, database, market_data,
services), per the plan's rule against self-calling the MCP over HTTP.
"""
