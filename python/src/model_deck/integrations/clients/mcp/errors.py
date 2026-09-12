from __future__ import annotations


class McpReadError(Exception):
    """Agent-visible MCP read failure; never includes secrets."""
