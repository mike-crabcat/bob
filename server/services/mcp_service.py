"""MCP client support: external tool servers as native LLM tools.

Security posture (Mike's decisions, 2026-09-14):
- Servers are registered ONLY through the token-gated dashboard API. The
  agent gets tools, never registration powers (the agent's bash can curl
  this API — that incident is why writes are token-gated and GETs redact
  env/header values).
- stdio servers run as this user, outside the workspace sandbox — a stdio
  command is arbitrary code Mike trusts per server. Children NEVER inherit
  the service environment (it holds LLM keys): they get the
  env_allowlist vars plus the per-server env_json, nothing else.

Session model: connection-per-call. The mcp SDK's transports are anyio
cancel-scoped context managers that must be exited in the task that entered
them, so persistent sessions inside FastAPI would need task-bridging
machinery for near-zero benefit at chat QPS. Every call_tool / refresh
opens → initializes → operates → closes under a timeout; a stdio child's
lifetime is bounded by that timeout, and nothing needs to survive a
`systemctl --user restart`. Tool *listing* never pays the connect cost:
definitions come from the TTL-refreshed cache, which the sync turn-path
factory reads.

Wire shape: MCP tools surface as ordinary Tool objects named
mcp_<server>_<tool>, so llm_call_log tools_json, tool-trace persistence
and replay, and folding all work unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx
from collections.abc import Collection, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from server.services.tools import Tool

logger = logging.getLogger(__name__)

_SERVER_NAME_RE = re.compile(r"[^a-z0-9_-]")
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
_MAX_WIRE_NAME = 64
_MAX_DESCRIPTION = 2000
_SERVER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


def validate_mcp_server_fields(body: dict, *,
                               partial: bool = False) -> tuple[dict | None, str]:
    """Validate MCP server fields from an untrusted writer (dashboard API
    POST/PUT, or the LLM's register tool). Returns (fields, error) — a None
    fields payload with a non-empty error means reject. Shared so the wire
    contract can't drift between the two registration paths."""
    fields: dict = {}
    name = body.get("name")
    if name is not None:
        if not _SERVER_SLUG_RE.match(str(name)):
            return None, ("name must match ^[a-z0-9][a-z0-9_-]{1,31}$ "
                          "(it names the tools: mcp_<name>_<tool>)")
        fields["name"] = str(name)
    elif not partial:
        return None, "name is required"

    transport = body.get("transport")
    if transport is not None:
        if transport not in ("stdio", "http"):
            return None, "transport must be 'stdio' or 'http'"
        fields["transport"] = transport
    elif not partial:
        return None, "transport is required"

    command = body.get("command")
    if command is not None:
        command = str(command)
        if command and not command.startswith("/"):
            return None, ("command must be an absolute path (the systemd "
                          "user service PATH is minimal; use e.g. "
                          "/usr/bin/npx or /home/bob/.nvm/.../npx)")
        fields["command"] = command

    url = body.get("url")
    if url is not None:
        url = str(url)
        if url and not url.startswith(("http://", "https://")):
            return None, "url must start with http:// or https://"
        fields["url"] = url

    for key in ("args", "env", "headers", "tool_filter"):
        value = body.get(key)
        if value is not None:
            if key in ("env", "headers") and not isinstance(value, dict):
                return None, f"{key} must be an object of key/value pairs"
            if key == "args" and not isinstance(value, list):
                return None, "args must be a list of strings"
            if key == "tool_filter" and not isinstance(value, dict):
                return None, "tool_filter must be {\"allow\":[...],\"deny\":[...]}"
            fields[key] = value

    for key in ("is_global", "trusted_only"):
        value = body.get(key)
        if value is not None:
            fields[key] = bool(value)
    if body.get("enabled") is not None:
        fields["enabled"] = bool(body["enabled"])
    if "timeout_seconds" in body:
        value = body["timeout_seconds"]
        if value is not None and (not isinstance(value, (int, float))
                                  or value <= 0):
            return None, "timeout_seconds must be a positive number or null"
        fields["timeout_seconds"] = value
    if body.get("note") is not None:
        fields["note"] = str(body["note"])

    if not partial:
        transport = fields.get("transport", "")
        if transport == "stdio" and not fields.get("command"):
            return None, "stdio servers need a command"
        if transport == "http" and not fields.get("url"):
            return None, "http servers need a url"
    return fields, ""


@dataclass
class ServerToolCache:
    """One server's cached tool definitions + last refresh outcome."""

    server_id: str
    name: str
    fetched_at: float = 0.0            # time.monotonic(); 0 = never succeeded
    tools: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def _sanitize_server_name(name: str) -> str:
    return _SERVER_NAME_RE.sub("", name.strip().lower()) or "server"


def _sanitize_tool_name(name: str) -> str:
    return _TOOL_NAME_RE.sub("_", name.strip()) or "tool"


def _truncate_output(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head, tail = cap * 3 // 4, cap // 4
    dropped = len(text) - head - tail
    return f"{text[:head]}…[truncated {dropped} chars]…{text[-tail:]}"


_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(value: str, *, context: str) -> str:
    """Expand ``${VAR}`` references from the service environment.

    Registration (dashboard or LLM tool) stores REFERENCES, not secret
    values — Bob can't read the service env, and the approval summary +
    dashboard GETs stay secret-free. Values expand here, at connect time.
    A literal ``$`` without braces is left alone so plain secrets with
    dollar signs keep working."""
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise ValueError(
                f"{context}: environment variable ${{{name}}} is not set — "
                f"add it to the service environment (~/config/.env) or "
                f"store the literal value")
        return resolved
    return _ENV_REF_RE.sub(repl, value)


def _root_cause(exc: BaseException) -> str:
    """The SDK wraps transport failures in anyio ExceptionGroups; the
    actionable message ('1001 auth failed', 'ConnectError') is at the
    leaf. Without this, every cache error reads 'ExceptionGroup: unhandled
    errors in a TaskGroup' and tells nobody anything."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


class McpManager:
    """Owns MCP server connections, the tool-definition cache, and the
    background refresh worker. Inert until servers are registered."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._cache: dict[str, ServerToolCache] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._sdk: Any | None = None       # resolved SDK symbols, or False
        self._sdk_warned = False

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._worker is not None:
            return
        from server.repositories.mcp import McpServerRepository
        rows = await McpServerRepository(self.ctx.db).list()
        await self.refresh_all(rows)
        self._worker = asyncio.create_task(
            self._refresh_loop(), name="mcp-manager-refresh")

    async def stop(self) -> None:
        self._stop.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    def _settings(self) -> Any:
        return self.ctx.settings.mcp

    # ── SDK + sessions ────────────────────────────────────────────────

    def _sdk_symbols(self) -> Any | None:
        """Lazily import the mcp SDK; None (cached as False) when absent."""
        if self._sdk is not None:
            return self._sdk or None
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client

            self._sdk = SimpleNamespace(
                ClientSession=ClientSession,
                StdioServerParameters=StdioServerParameters,
                stdio_client=stdio_client,
                streamable_http_client=streamable_http_client,
            )
        except ImportError:
            self._sdk = False
            if not self._sdk_warned:
                self._sdk_warned = True
                logger.warning(
                    "mcp SDK not installed — MCP servers disabled "
                    "(uv add mcp)")
        return self._sdk or None

    def _child_env(self, row: dict[str, Any]) -> dict[str, str]:
        """Allowlisted vars + the server's env_json — never os.environ
        wholesale: the service env carries LLM keys and the API secret.
        env_json values may carry ${VAR} references (expanded from the
        service env — that's the one place a child may see a secret)."""
        allow = {v.strip() for v in self._settings().env_allowlist.split(",")}
        env = {k: os.environ[k] for k in allow if os.environ.get(k)}
        try:
            extra = json.loads(row.get("env_json") or "{}")
            if isinstance(extra, dict):
                for key, value in extra.items():
                    env[str(key)] = _expand_env_refs(
                        str(value), context=f"mcp server {row['name']} env")
        except (ValueError, TypeError):
            logger.warning("mcp server %s: unparseable env_json", row["name"])
        return env

    def _http_headers(self, row: dict[str, Any]) -> dict[str, str] | None:
        """Auth headers for http servers, with ${VAR} references expanded
        from the service environment (the registration stores the
        reference, never the secret)."""
        try:
            headers = json.loads(row.get("headers_json") or "{}")
        except (ValueError, TypeError):
            logger.warning("mcp server %s: unparseable headers_json",
                           row["name"])
            return None
        if not isinstance(headers, dict) or not headers:
            return None
        return {str(k): _expand_env_refs(str(v),
                                         context=f"mcp server {row['name']} headers")
                for k, v in headers.items()}

    @asynccontextmanager
    async def _open_session(self, row: dict[str, Any]):
        """One fresh client session (see module docstring: per-call)."""
        sdk = self._sdk_symbols()
        if sdk is None:
            raise RuntimeError("mcp SDK not installed (uv add mcp)")
        if row["transport"] == "stdio":
            params = sdk.StdioServerParameters(
                command=row["command"],
                args=json.loads(row.get("args_json") or "[]"),
                env=self._child_env(row))
            async with sdk.stdio_client(params) as (read, write):
                async with sdk.ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            # mcp 2.x: custom headers ride an injected httpx client (client
            # default headers merge into every request the transport makes);
            # the transport yields (read, write)
            http_client = httpx.AsyncClient(
                headers=self._http_headers(row),
                timeout=self._settings().connect_timeout_seconds)
            try:
                async with sdk.streamable_http_client(
                        row["url"], http_client=http_client) as (read, write):
                    async with sdk.ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            finally:
                await http_client.aclose()

    # ── refresh + cache ───────────────────────────────────────────────

    async def refresh_all(self, rows: Iterable[dict[str, Any]] | None = None) -> None:
        from server.repositories.mcp import McpServerRepository
        if rows is None:
            rows = [r for r in await McpServerRepository(self.ctx.db).list()
                    if r["enabled"]]
        for row in rows:
            if not row["enabled"]:
                continue
            try:
                await self.refresh_server(row)
            except Exception:
                logger.exception("mcp refresh failed for %s", row.get("name"))

    async def refresh_server(self, row: dict[str, Any]) -> ServerToolCache:
        lock = self._locks.setdefault(row["id"], asyncio.Lock())
        async with lock:
            cache = self._cache.get(row["id"])
            if cache is None:
                cache = ServerToolCache(server_id=row["id"], name=row["name"])
                self._cache[row["id"]] = cache
            cache.name = row["name"]
            try:
                async with asyncio.timeout(self._settings().connect_timeout_seconds):
                    async with self._open_session(row) as session:
                        result = await session.list_tools()
                tools = self._apply_filters(row, result.tools)
                # Clear before caps: _enforce_caps raises its own degradation
                # notes, and clearing after would wipe them (the 2026-09-17
                # elevenlabs bug — a budget-failed server rendered as a bare
                # "0 tools" with no reason).
                cache.error = ""
                self._enforce_caps(cache, tools)
                cache.fetched_at = time.monotonic()
            except Exception as exc:
                # Keep serving stale tools (if any) — better than yanking a
                # working server's tools over a transient connect failure.
                cache.error = _root_cause(exc)
                logger.warning("mcp list_tools failed for %s: %s",
                               row["name"], cache.error)
            return cache

    def _apply_filters(self, row: dict[str, Any],
                       raw_tools: Iterable[Any]) -> list[dict[str, Any]]:
        try:
            flt = json.loads(row.get("tool_filter_json") or "{}") or {}
        except ValueError:
            flt = {}
        allow = set(flt.get("allow") or [])
        deny = set(flt.get("deny") or [])
        tools: list[dict[str, Any]] = []
        for t in raw_tools:
            if allow and t.name not in allow:
                continue
            if t.name in deny:
                continue
            tools.append({
                "name": t.name,
                "description": getattr(t, "description", None) or "",
                # mcp 2.x snake_cases inputSchema; 1.x used camelCase
                "input_schema": (getattr(t, "input_schema", None)
                                 or getattr(t, "inputSchema", None) or {}),
            })
        return tools

    def _enforce_caps(self, cache: ServerToolCache,
                      tools: list[dict[str, Any]]) -> None:
        """Degrade, don't vanish. A server over the schema budget loses
        verbose descriptions first (they dominate the bulk, and the wire
        caps them anyway), then its tail tools — never the whole suite,
        which is how the elevenlabs server sat at "0 tools" 2026-09-17 and
        pushed Bob into a doomed script fallback."""
        s = self._settings()
        if len(tools) > s.max_tools_per_server:
            logger.warning("mcp %s: %d tools over cap %d — truncating",
                           cache.name, len(tools), s.max_tools_per_server)
            tools = tools[:s.max_tools_per_server]
        budget = s.max_schema_chars_per_server
        total = sum(len(json.dumps(t)) for t in tools)
        if total <= budget:
            cache.tools = tools
            return
        cut = s.max_tool_description_chars
        slimmed = [dict(t, description=t.get("description", "")[:cut])
                   for t in tools]
        if sum(len(json.dumps(t)) for t in slimmed) <= budget:
            logger.warning("mcp %s: %d chars over budget %d — descriptions "
                           "trimmed to %d chars, all %d tools kept",
                           cache.name, total, budget, cut, len(tools))
            cache.tools = slimmed
            cache.error = (f"schema budget tight — descriptions trimmed to "
                           f"{cut} chars")
            return
        kept: list[dict[str, Any]] = []
        used = 0
        for t in slimmed:
            size = len(json.dumps(t))
            if used + size > budget:
                break
            kept.append(t)
            used += size
        if not kept:
            logger.warning("mcp %s: %d chars over budget %d — no tool fits",
                           cache.name, total, budget)
            cache.tools = []
            cache.error = (f"schema budget exceeded ({total} chars > "
                           f"{budget}) and no single tool fits — tighten "
                           f"tool_filter_json")
            return
        logger.warning("mcp %s: %d chars over budget %d even trimmed — "
                       "kept %d of %d tools",
                       cache.name, total, budget, len(kept), len(tools))
        cache.tools = kept
        cache.error = (f"schema budget — exposing {len(kept)} of {len(tools)} "
                       f"tools; tighten tool_filter_json to choose which")

    async def refresh_due(self) -> None:
        ttl = self._settings().refresh_interval_seconds
        now = time.monotonic()
        due = [c for c in self._cache.values()
               if now - c.fetched_at >= ttl and not c.error]
        # also keep retrying errored servers, at the same cadence
        due += [c for c in self._cache.values() if c.error]
        if not due:
            return
        from server.repositories.mcp import McpServerRepository
        repo = McpServerRepository(self.ctx.db)
        for cache in due:
            row = await repo.get(cache.server_id)
            if row is None or not row["enabled"]:
                self._cache.pop(cache.server_id, None)
                continue
            try:
                await self.refresh_server(row)
            except Exception:
                logger.exception("mcp refresh failed for %s", cache.name)

    async def reload(self) -> None:
        """After CRUD: re-read rows, drop caches for removed/disabled
        servers, then refresh what's live."""
        from server.repositories.mcp import McpServerRepository
        rows = await McpServerRepository(self.ctx.db).list()
        live = {r["id"]: r for r in rows if r["enabled"]}
        for server_id in list(self._cache):
            if server_id not in live:
                self._cache.pop(server_id, None)
        await self.refresh_all(list(live.values()))

    async def _refresh_loop(self) -> None:
        interval = max(self._settings().refresh_interval_seconds / 2, 5.0)
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop set
            except asyncio.TimeoutError:
                pass
            try:
                await self.refresh_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("mcp refresh cycle failed")

    # ── turn path (sync, cache-only) ──────────────────────────────────

    def tools_for(self, server_rows: Iterable[dict[str, Any]],
                  *, reserved: Collection[str] = ()) -> list[Tool]:
        seen: set[str] = set(reserved)
        tools: list[Tool] = []
        for row in server_rows:
            cache = self._cache.get(row["id"])
            if cache is None or not cache.tools:
                continue
            prefix = _sanitize_server_name(row["name"])
            for d in cache.tools:
                wire = f"mcp_{prefix}_{_sanitize_tool_name(d['name'])}"[:_MAX_WIRE_NAME]
                if wire in seen:
                    logger.warning("mcp tool name collision, skipping: %s", wire)
                    continue
                seen.add(wire)
                schema = d.get("input_schema") or {}
                props = {k: v for k, v in (schema.get("properties") or {}).items()
                         if isinstance(v, dict)}
                description = (f"{d.get('description') or ''} "
                               f"[MCP server: {row['name']}]").strip()
                tools.append(Tool(
                    name=wire,
                    description=description[:_MAX_DESCRIPTION],
                    parameters=props,
                    required=[r for r in (schema.get("required") or [])
                              if r in props],
                    handler=self._make_handler(row["name"], d["name"]),
                ))
        return tools

    def _make_handler(self, server_name: str, tool_name: str):
        async def handler(**kwargs: Any) -> str:
            try:
                return await self.call_tool(server_name, tool_name, kwargs)
            except Exception as exc:
                cause = _root_cause(exc)
                logger.warning("mcp tool %s/%s failed: %s",
                               server_name, tool_name, cause)
                return (f"Error: MCP tool {tool_name} ({server_name}) "
                        f"failed: {cause}")
        return handler

    # ── execution ─────────────────────────────────────────────────────

    async def call_tool(self, server_name: str, tool_name: str,
                        args: dict[str, Any]) -> str:
        from server.repositories.mcp import McpServerRepository
        row = await McpServerRepository(self.ctx.db).get_by_name(server_name)
        if row is None or not row["enabled"]:
            return (f"Error: MCP server '{server_name}' is no longer "
                    f"registered or enabled")
        timeout = row["timeout_seconds"] or self._settings().default_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                async with self._open_session(row) as session:
                    result = await session.call_tool(tool_name, arguments=args)
        except TimeoutError:
            logger.warning("mcp tool %s/%s timed out after %ss",
                           server_name, tool_name, timeout)
            return (f"Error: MCP tool {tool_name} ({server_name}) timed "
                    f"out after {timeout}s")
        return self._flatten_result(result)

    def _flatten_result(self, result: Any) -> str:
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
            elif getattr(block, "type", None) == "text":
                parts.append(str(block))
            else:
                parts.append("[unsupported content block omitted]")
        structured = getattr(result, "structuredContent", None)
        if structured:
            parts.append(json.dumps(structured, default=str))
        text = "\n".join(p for p in parts if p)
        if getattr(result, "isError", None):
            text = f"Error: {text or 'MCP tool returned an error'}"
        return _truncate_output(text or "(empty result)",
                                self._settings().max_output_chars)

    # ── dashboard surface ─────────────────────────────────────────────

    async def test_server(self, row: dict[str, Any]) -> dict[str, Any]:
        """Fresh-connection health check, bypassing the cache."""
        try:
            async with asyncio.timeout(self._settings().connect_timeout_seconds):
                async with self._open_session(row) as session:
                    result = await session.list_tools()
            return {"ok": True,
                    "tools": [t.name for t in result.tools],
                    "error": ""}
        except Exception as exc:
            return {"ok": False, "tools": [], "error": _root_cause(exc)}

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        out = []
        for cache in self._cache.values():
            out.append({
                "server_id": cache.server_id,
                "name": cache.name,
                "tool_count": len(cache.tools),
                "age_seconds": (round(now - cache.fetched_at, 1)
                                if cache.fetched_at else None),
                "error": cache.error,
            })
        return out

    def cached_tools(self, server_id: str) -> list[dict[str, Any]]:
        cache = self._cache.get(server_id)
        return list(cache.tools) if cache else []


async def make_mcp_tools(ctx: Any, *, session_key: str,
                         is_trusted: bool = False,
                         reserved: Collection[str] = ()) -> list[Tool]:
    """Per-turn MCP tool resolution: global servers + this conversation's
    attachments, minus trusted_only ones in untrusted sessions. Reads the
    manager's cache (sync) after the async DB scoping. A missing manager
    (tests, ad-hoc contexts) or a disabled subsystem is a clean no-op."""
    manager = getattr(ctx, "mcp", None)
    if manager is None or not ctx.settings.mcp.enabled:
        return []
    from server.repositories.conversations import ConversationRepository
    from server.repositories.mcp import McpServerRepository
    cid = await ConversationRepository(ctx.db).resolve_cid(session_key)
    rows = await McpServerRepository(ctx.db).servers_for_conversation(cid)
    if not is_trusted:
        rows = [r for r in rows if not r["trusted_only"]]
    if not rows:
        return []
    return manager.tools_for(rows, reserved=reserved)
