"""McpManager unit tests: cache→Tool conversion, caps/filters, handler
wrapper, make_mcp_tools scoping — all without the mcp SDK or any server."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.config import Settings
from server.repositories.mcp import McpServerRepository
from server.services.mcp_service import (
    McpManager, ServerToolCache, _truncate_output, make_mcp_tools,
)


def _manager(settings: Settings | None = None, db=None) -> McpManager:
    """Bare manager with just the fields tools_for/handlers touch."""
    manager = object.__new__(McpManager)
    manager.ctx = SimpleNamespace(db=db, settings=settings or Settings.from_env())
    manager._cache = {}
    manager._locks = {}
    manager._stop = asyncio.Event()
    manager._worker = None
    manager._sdk = False          # SDK-absent path
    manager._sdk_warned = True    # silence the one-time warning
    return manager


def _cache(server_id: str, name: str, tools: list[dict],
           error: str = "") -> ServerToolCache:
    return ServerToolCache(server_id=server_id, name=name, tools=tools,
                           error=error)


def _row(server_id: str, name: str, **extra) -> dict:
    return {"id": server_id, "name": name, "enabled": 1, "transport": "stdio",
            "timeout_seconds": None, **extra}


# ── tools_for: conversion ─────────────────────────────────────────────

async def test_tools_for_namespacing_and_schema():
    manager = _manager()
    manager._cache["s1"] = _cache("s1", "fetch", [{
        "name": "getPage", "description": "Fetch a page",
        "input_schema": {"properties": {"url": {"type": "string"},
                                        "raw": {"type": "boolean"}},
                         "required": ["url", "missing_prop"],
                         "additionalProperties": False, "$schema": "x"},
    }])
    tools = manager.tools_for([_row("s1", "fetch")])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "mcp_fetch_getPage"
    assert set(tool.parameters) == {"url", "raw"}   # envelope keys dropped
    assert tool.required == ["url"]                 # missing_prop filtered
    assert "[MCP server: fetch]" in tool.description
    assert tool.to_openai_format()["type"] == "function"


async def test_tools_for_reserved_collision_skipped():
    manager = _manager()
    manager._cache["s1"] = _cache("s1", "fetch", [
        {"name": "echo", "description": "d", "input_schema": {}}])
    tools = manager.tools_for([_row("s1", "fetch")],
                              reserved={"mcp_fetch_echo"})
    assert tools == []  # never shadow an existing tool name


async def test_tools_for_sanitizes_and_caps_name():
    manager = _manager()
    manager._cache["s1"] = _cache("s1", "My Server!", [
        {"name": "weird tool/name!!", "description": "d",
         "input_schema": {}}])
    tools = manager.tools_for([_row("s1", "My Server!")])
    assert tools[0].name == "mcp_myserver_weird_tool_name__"
    assert len(tools[0].name) <= 64


async def test_tools_for_skips_empty_serves_stale_on_error():
    manager = _manager()
    manager._cache["empty"] = _cache("empty", "empty", [])
    # errored refresh keeps stale tools (stale beats yanking a working
    # server's tools over a transient connect failure)
    manager._cache["err"] = _cache("err", "err", [{"name": "t",
                                                   "input_schema": {}}],
                                   error="connect failed")
    manager._cache["fresh"] = _cache("fresh", "fresh", [
        {"name": "t", "description": "", "input_schema": {}}])
    tools = manager.tools_for([_row("empty", "e"), _row("err", "err"),
                               _row("fresh", "fresh")])
    assert [t.name for t in tools] == ["mcp_err_t", "mcp_fresh_t"]


# ── caps and filters ─────────────────────────────────────────────────

async def test_enforce_caps_tools_truncated_schema_budget_degrades():
    settings = Settings.from_env()
    settings.mcp.max_tools_per_server = 2
    settings.mcp.max_schema_chars_per_server = 10_000
    manager = _manager(settings)
    cache = _cache("s1", "big", [])
    tools = [{"name": f"t{i}", "description": "x" * 50, "input_schema": {}}
             for i in range(5)]
    manager._enforce_caps(cache, tools)
    assert len(cache.tools) == 2

    # schema over budget and trimming can't save it → keep the prefix that
    # fits, never zero (the 2026-09-17 elevenlabs "0 tools" bug). Each tool
    # json.dumps to ~123 chars: 150 fits one, not two.
    settings.mcp.max_tools_per_server = 80
    settings.mcp.max_schema_chars_per_server = 150
    manager._enforce_caps(cache, tools)
    assert len(cache.tools) == 1
    assert cache.tools[0]["name"] == "t0"
    assert "exposing 1 of 5" in cache.error


async def test_enforce_caps_budget_trims_descriptions_first():
    """Descriptions dominate schema bulk — an over-budget server whose
    bloat is prose keeps every tool with descriptions cut."""
    settings = Settings.from_env()
    settings.mcp.max_schema_chars_per_server = 1_000
    settings.mcp.max_tool_description_chars = 100
    manager = _manager(settings)
    cache = _cache("s1", "elevenlabs", [])
    tools = [{"name": f"tool_{i}", "description": "x" * 400,
              "input_schema": {"properties": {"a": {"type": "string"}}}}
             for i in range(4)]           # ~450 chars each raw ≈ 1800 total
    manager._enforce_caps(cache, tools)
    assert len(cache.tools) == 4           # all kept, none dropped
    assert all(len(t["description"]) <= 100 for t in cache.tools)
    assert "descriptions trimmed" in cache.error


async def test_enforce_caps_no_tool_fits_still_explains():
    settings = Settings.from_env()
    settings.mcp.max_schema_chars_per_server = 20
    manager = _manager(settings)
    cache = _cache("s1", "monolith", [])
    manager._enforce_caps(
        cache, [{"name": "huge", "description": "x",
                 "input_schema": {"properties": {"a": {"type": "string"}}}}])
    assert cache.tools == []
    assert "no single tool fits" in cache.error


async def test_apply_filters_allow_deny():
    manager = _manager()

    class T:
        def __init__(self, name):
            self.name = name
            self.description = name
            self.inputSchema = {"properties": {}}

    row = _row("s1", "s", tool_filter_json='{"allow": ["a", "b"], "deny": ["b"]}')
    got = manager._apply_filters(row, [T("a"), T("b"), T("c")])
    assert [t["name"] for t in got] == ["a"]


# ── handler wrapper + result flattening ──────────────────────────────

async def test_handler_passthrough_and_error():
    manager = _manager()

    async def ok_call(server, tool, args):
        assert (server, tool, args) == ("fetch", "getPage", {"url": "x"})
        return "the page"

    manager.call_tool = ok_call  # type: ignore[method-assign]
    handler = manager._make_handler("fetch", "getPage")
    assert await handler(url="x") == "the page"

    async def boom(server, tool, args):
        raise RuntimeError("nope")

    manager.call_tool = boom  # type: ignore[method-assign]
    handler = manager._make_handler("fetch", "getPage")
    out = await handler(url="x")
    assert out.startswith(
        "Error: MCP tool getPage (fetch) failed: RuntimeError: nope")


async def test_flatten_result_text_structured_error():
    manager = _manager()

    text_block = SimpleNamespace(text="hello")
    image_block = SimpleNamespace(type="image")  # no .text
    result = SimpleNamespace(content=[text_block, image_block],
                             isError=False, structuredContent={"k": 1})
    out = manager._flatten_result(result)
    assert "hello" in out and "[unsupported content block omitted]" in out
    assert '"k": 1' in out

    err = SimpleNamespace(content=[text_block], isError=True,
                          structuredContent=None)
    assert manager._flatten_result(err).startswith("Error:")


async def test_truncate_output_shape():
    text = "a" * 30_000
    out = _truncate_output(text, 1_000)
    assert len(out) < 1_200
    assert "truncated" in out
    assert _truncate_output("short", 100) == "short"


async def test_child_env_never_inherits_service_env(monkeypatch):
    monkeypatch.setenv("BOB_OPENAI_API_KEY", "sk-super-secret")
    monkeypatch.setenv("HOME", "/home/bob")
    monkeypatch.setenv("TZ", "Australia/Perth")
    manager = _manager()
    row = _row("s1", "s", env_json='{"API_KEY": "server-key"}')
    env = manager._child_env(row)
    assert "BOB_OPENAI_API_KEY" not in env      # service env never leaks
    assert env["API_KEY"] == "server-key"       # per-server env applied
    assert env["HOME"] == "/home/bob" and env["TZ"] == "Australia/Perth"


# ── make_mcp_tools: scoping + no-op branches ─────────────────────────

async def test_make_mcp_tools_scoping(db):
    settings = Settings.from_env()
    manager = _manager(settings, db=db)
    ctx = SimpleNamespace(db=db, settings=settings, mcp=manager)
    repo = McpServerRepository(db)
    glob = await repo.create(name="glob", transport="stdio",
                             command="/bin/true", is_global=True)
    dm_only = await repo.create(name="dm-only", transport="stdio",
                                command="/bin/true", trusted_only=True,
                                is_global=True)
    await repo.create(name="unattached", transport="stdio", command="/bin/true")
    attached = await repo.create(name="attached", transport="stdio",
                                 command="/bin/true")
    await db.execute(
        "INSERT INTO conversations (id, kind, created_at, updated_at) "
        "VALUES ('sk-1', 'dm', '2026-09-14', '2026-09-14')")
    await repo.set_attachments("sk-1", [attached["id"]])

    manager._cache[glob["id"]] = _cache(glob["id"], "glob", [
        {"name": "t", "description": "d", "input_schema": {}}])
    manager._cache[dm_only["id"]] = _cache(dm_only["id"], "dm-only", [
        {"name": "t", "description": "d", "input_schema": {}}])
    manager._cache[attached["id"]] = _cache(attached["id"], "attached", [
        {"name": "t", "description": "d", "input_schema": {}}])

    trusted = await make_mcp_tools(ctx, session_key="sk-1", is_trusted=True)
    assert [t.name for t in trusted] == [
        "mcp_attached_t", "mcp_dm-only_t", "mcp_glob_t"]  # ORDER BY name

    untrusted = await make_mcp_tools(ctx, session_key="sk-1", is_trusted=False)
    assert [t.name for t in untrusted] == ["mcp_attached_t", "mcp_glob_t"]

    other = await make_mcp_tools(ctx, session_key="agent:main:unbound",
                                 is_trusted=True)
    assert [t.name for t in other] == ["mcp_dm-only_t", "mcp_glob_t"]


async def test_make_mcp_tools_noop_branches(db):
    settings = Settings.from_env()
    # no manager on ctx (tests, ad-hoc contexts)
    ctx = SimpleNamespace(db=db, settings=settings, mcp=None)
    assert await make_mcp_tools(ctx, session_key="any") == []
    # subsystem disabled
    settings.mcp.enabled = False
    ctx2 = SimpleNamespace(db=db, settings=settings,
                           mcp=_manager(settings, db=db))
    assert await make_mcp_tools(ctx2, session_key="any") == []


async def test_refresh_due_drops_removed_servers(db):
    manager = _manager(Settings.from_env(), db=db)
    repo = McpServerRepository(db)
    gone = await repo.create(name="gone", transport="stdio", command="/bin/true")
    manager._cache[gone["id"]] = _cache(gone["id"], "gone", [], error="stale")
    await repo.delete(gone["id"])
    await manager.refresh_due()
    assert gone["id"] not in manager._cache


# ── ${VAR} reference expansion + root-cause errors (zai incident) ────

async def test_expand_env_refs(monkeypatch):
    from server.services.mcp_service import _expand_env_refs

    monkeypatch.setenv("BOB_TEST_KEY", "tok-1")
    monkeypatch.setenv("A", "v")
    assert _expand_env_refs("Bearer ${BOB_TEST_KEY}",
                            context="test") == "Bearer tok-1"
    assert _expand_env_refs("${A}-${A}", context="t") == "v-v"
    # literal $ without braces is left alone (plain secrets keep working)
    assert _expand_env_refs("$notaref and $9", context="t") == "$notaref and $9"
    assert _expand_env_refs("plain $x$y", context="t") == "plain $x$y"


async def test_expand_env_refs_missing_var_raises(monkeypatch):
    from server.services.mcp_service import _expand_env_refs

    monkeypatch.delenv("BOB_MISSING_KEY", raising=False)
    try:
        _expand_env_refs("${BOB_MISSING_KEY}", context="mcp server x headers")
        raised = False
    except ValueError as exc:
        raised = True
        assert "BOB_MISSING_KEY" in str(exc) and "x headers" in str(exc)
    assert raised


async def test_http_headers_expansion_and_child_env_refs(monkeypatch):
    monkeypatch.setenv("BOB_ZAI_API_KEY", "zai-tok")
    monkeypatch.setenv("SECRET_TWO", "two")
    manager = _manager()
    row = _row("s1", "zai-web-search",
               headers_json='{"Authorization": "Bearer ${BOB_ZAI_API_KEY}"}',
               env_json='{"K": "${SECRET_TWO}"}')
    assert manager._http_headers(row) == {"Authorization": "Bearer zai-tok"}
    assert manager._child_env(row)["K"] == "two"
    # empty/missing headers → None; unparseable → None, no raise
    assert manager._http_headers(_row("s2", "s")) is None
    assert manager._http_headers(_row("s3", "s", headers_json="not json")) is None


async def test_root_cause_unwraps_exception_groups():
    from server.services.mcp_service import _root_cause

    leaf = ValueError("1001: Authentication failed")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup",
                             [ExceptionGroup("inner", [leaf])])
    assert _root_cause(wrapped) == "ValueError: 1001: Authentication failed"
    assert _root_cause(RuntimeError("plain")) == "RuntimeError: plain"


async def test_refresh_error_shows_root_cause(db):
    from contextlib import asynccontextmanager

    manager = _manager(Settings.from_env(), db=db)

    @asynccontextmanager
    async def fake_session(row):
        raise ExceptionGroup("taskgroup", [RuntimeError("auth rejected 1001")])
        yield  # pragma: no cover

    manager._sdk = True  # pretend the SDK exists so _open_session isn't hit
    manager._open_session = fake_session  # type: ignore[method-assign]
    row = _row("s1", "broken", transport="http", url="https://x.test/mcp")
    cache = await manager.refresh_server(row)
    assert "auth rejected 1001" in cache.error
    assert "ExceptionGroup" not in cache.error
