"""Minimal MCP stdio server for the integration test (run: python -u this).

Exposes one tool: echo(text) -> text with a marker prefix, so the test can
tell a real round-trip from a cache artifact. Uses the mcp 2.x MCPServer
API (FastMCP's renamed successor).
"""

import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("bob-test-echo")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back with a marker prefix."""
    return f"echoed: {text}"


if __name__ == "__main__":
    server.run(transport="stdio")
    sys.exit(0)
