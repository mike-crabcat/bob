"""Dashboard API for MCP servers (registration is Mike-only, 2026-09-14).

Writes are token-gated by the global ApiAuthMiddleware plus _check_auth
here. GETs pass that gate unauthenticated — the agent's bash can curl them
— so every response masks env/header VALUES as "***" (keys preserved);
a "***" value on PUT means keep the stored one.
"""

from __future__ import annotations

import json

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405
from server.repositories.mcp import McpServerRepository
from server.services.mcp_service import validate_mcp_server_fields

router = APIRouter()

_REDACTED = "***"


def _manager(request: Request):
    return getattr(request.app.state, "mcp_manager", None)


def _redact(row: dict) -> dict:
    out = dict(row)
    for col in ("env_json", "headers_json"):
        try:
            values = json.loads(out.get(col) or "{}")
        except ValueError:
            values = {}
        out[col] = {k: _REDACTED for k in values}
    return out


def _with_status(rows: list[dict], manager) -> list[dict]:
    status = {s["server_id"]: s for s in (manager.status() if manager else [])}
    out = []
    for row in rows:
        redacted = _redact(row)
        s = status.get(row["id"], {})
        redacted["tool_count"] = s.get("tool_count", 0)
        redacted["cache_age_seconds"] = s.get("age_seconds")
        redacted["cache_error"] = s.get("error", "")
        out.append(redacted)
    return out


@router.get("/api/mcp/servers")
async def list_mcp_servers(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    repo = McpServerRepository(_db(request))
    rows = await repo.list()
    return {"servers": _with_status(rows, _manager(request)),
            "manager_started": _manager(request) is not None}


@router.post("/api/mcp/servers")
async def create_mcp_server(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    fields, error = validate_mcp_server_fields(body)
    if error:
        return {"error": error}
    repo = McpServerRepository(_db(request))
    if await repo.get_by_name(fields["name"]):
        return {"error": f"a server named '{fields['name']}' already exists"}
    row = await repo.create(**fields)
    manager = _manager(request)
    if manager is not None and row["enabled"]:
        await manager.refresh_server(row)
    return {"server": _with_status([row], manager)[0]}


@router.get("/api/mcp/servers/{server_id}")
async def get_mcp_server(request: Request, server_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    row = await McpServerRepository(_db(request)).get(server_id)
    if row is None:
        return {"error": "not found"}
    manager = _manager(request)
    out = _with_status([row], manager)[0]
    if manager is not None:
        out["tools"] = manager.cached_tools(server_id)
    else:
        out["tools"] = []
    return {"server": out}


@router.put("/api/mcp/servers/{server_id}")
async def update_mcp_server(request: Request, server_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    repo = McpServerRepository(_db(request))
    if await repo.get(server_id) is None:
        return {"error": "not found"}
    body = await request.json()
    fields, error = validate_mcp_server_fields(body, partial=True)
    if error:
        return {"error": error}
    if not fields:
        return {"error": "no recognized fields to update"}
    row = await repo.update(server_id, **fields)
    manager = _manager(request)
    if manager is not None and row and row["enabled"]:
        await manager.refresh_server(row)
    return {"server": _with_status([row], manager)[0]}


@router.delete("/api/mcp/servers/{server_id}")
async def delete_mcp_server(request: Request, server_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    deleted = await McpServerRepository(_db(request)).delete(server_id)
    if not deleted:
        return {"error": "not found"}
    manager = _manager(request)
    if manager is not None:
        await manager.reload()
    return {"ok": True}


@router.post("/api/mcp/servers/{server_id}/enabled")
async def set_mcp_server_enabled(request: Request,
                                 server_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    if not isinstance(body.get("enabled"), bool):
        return {"error": "body must be {\"enabled\": true|false}"}
    repo = McpServerRepository(_db(request))
    if not await repo.set_enabled(server_id, body["enabled"]):
        return {"error": "not found"}
    manager = _manager(request)
    if manager is not None:
        await manager.reload()
    return {"ok": True, "enabled": body["enabled"]}


@router.post("/api/mcp/servers/{server_id}/test")
async def test_mcp_server(request: Request, server_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    row = await McpServerRepository(_db(request)).get(server_id)
    if row is None:
        return {"error": "not found"}
    manager = _manager(request)
    if manager is None:
        return {"ok": False, "tools": [],
                "error": "MCP manager not running (BOB_MCP_ENABLED=off or "
                         "boot failure)"}
    result = await manager.test_server(row)
    if result["ok"]:
        # opportunistically adopt the fresh listing into the cache
        await manager.refresh_server(row)
    return result


@router.get("/api/conversations/{conversation_id:path}/mcp")
async def get_conversation_mcp(request: Request,
                               conversation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    repo = McpServerRepository(_db(request))
    attached = await repo.attachments_for(conversation_id)
    rows = await repo.list()
    attached_ids = {a["mcp_server_id"] for a in attached}
    return {
        "attached": [
            {"server_id": a["mcp_server_id"], "name": a["server_name"],
             "attached_by": a["attached_by"], "created_at": a["created_at"]}
            for a in attached],
        "available": [_redact(r) for r in rows
                      if r["id"] not in attached_ids],
    }


@router.put("/api/conversations/{conversation_id:path}/mcp")
async def set_conversation_mcp(request: Request,
                               conversation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    server_ids = body.get("server_ids")
    if not isinstance(server_ids, list) or not all(
            isinstance(s, str) for s in server_ids):
        return {"error": "body must be {\"server_ids\": [...]}"}
    repo = McpServerRepository(_db(request))
    known = {r["id"] for r in await repo.list()}
    unknown = [s for s in server_ids if s not in known]
    if unknown:
        return {"error": f"unknown server ids: {unknown}"}
    await repo.set_attachments(conversation_id, server_ids,
                               attached_by=body.get("attached_by", ""))
    return {"ok": True, "conversation_id": conversation_id,
            "server_ids": server_ids}
