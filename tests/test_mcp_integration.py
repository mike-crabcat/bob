"""MCP integration test: real stdio session against a local FastMCP echo
server. Skipped wherever the mcp SDK isn't installed (CI, agent sandbox)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from server.config import Settings  # noqa: E402
from server.context import AppContext  # noqa: E402
from server.repositories.mcp import McpServerRepository  # noqa: E402
from server.services.mcp_service import McpManager  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


async def test_stdio_round_trip(db):
    repo = McpServerRepository(db)
    row = await repo.create(
        name="testsrv", transport="stdio",
        command=sys.executable, args=["-u", str(FIXTURE)],
        env={"MARKER": "1"})
    settings = Settings.from_env()
    ctx = AppContext(db=db, settings=settings)
    manager = McpManager(ctx)

    cache = await manager.refresh_server(row)
    assert not cache.error, cache.error
    assert [t["name"] for t in cache.tools] == ["echo"]
    assert cache.tools[0]["input_schema"]["properties"]["text"]["type"] == "string"

    tools = manager.tools_for([row])
    assert [t.name for t in tools] == ["mcp_testsrv_echo"]
    tool = tools[0]
    assert tool.required == ["text"]

    # handler → fresh stdio session → echo round-trip
    result = await tool.handler(text="hello mcp")
    assert result == "echoed: hello mcp"

    # env_json reached the child (allowlisted env + per-server env)
    child_env = manager._child_env(row)
    assert child_env.get("MARKER") == "1"

    # status reflects the cache
    assert manager.status()[0]["tool_count"] == 1
