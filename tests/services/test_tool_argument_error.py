"""Tool calls with arguments the handler can't take get a clear retry
message instead of a TypeError crash (2026-10-13)."""

from __future__ import annotations

from server.services.tool_registry import build_common_tools
from server.services.tools import argument_error, tool


@tool
async def sample(name: str, days: int = 3) -> str:
    """Sample."""
    return name


def test_unknown_and_missing_arguments_explained():
    assert argument_error(sample.handler, {"name": "x"}) is None
    assert argument_error(sample.handler, {"name": "x", "days": 2}) is None
    err = argument_error(sample.handler, {"name": "x", "valid_from": "2026-10-13"})
    assert "no argument named 'valid_from'" in err and "Valid arguments: name, days" in err
    err = argument_error(sample.handler, {"days": 1})
    assert "missing required 'name'" in err


def test_kwargs_handlers_accept_anything():
    async def loose(**kw):
        return ""
    assert argument_error(loose, {"anything": 1}) is None


async def test_real_write_routine_rejects_valid_from(ctx):
    tools = {t.name: t for t in build_common_tools(
        ctx, session_key="agent:main:whatsapp:dm:61400000001", is_trusted=True)}
    err = argument_error(tools["write_routine"].handler,
                         {"routine_yaml": "name: x", "valid_from": "2026-10-13"})
    assert err and "'valid_from'" in err and "routine_yaml" in err
