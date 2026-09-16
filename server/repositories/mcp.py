"""SQL ownership for mcp_servers + conversation_mcp_attachments.

Mike-registered MCP tool servers (stdio subprocess or streamable HTTP),
exposed to the LLM as native tools namespaced mcp_<server>_<tool>. A server
is either global (every conversation) or attached per conversation. Writes
arrive only through the token-gated dashboard API — the agent never gets
registration powers. Everything touching these tables lives here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from server.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class McpServerRepository:
    def __init__(self, db: Database):
        self.db = db

    async def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.fetch_all(
            "SELECT * FROM mcp_servers ORDER BY name")]

    async def get(self, server_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM mcp_servers WHERE id = ?", (server_id,))

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM mcp_servers WHERE name = ?", (name,))

    async def create(
        self, *, name: str, transport: str, command: str = "",
        args: list[str] | None = None, env: dict[str, str] | None = None,
        url: str = "", headers: dict[str, str] | None = None,
        enabled: bool = True, is_global: bool = False,
        trusted_only: bool = False, tool_filter: dict[str, Any] | None = None,
        timeout_seconds: int | None = None, note: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        now = _now_iso()
        server_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO mcp_servers (id, name, transport, command, args_json, "
            "env_json, url, headers_json, enabled, is_global, trusted_only, "
            "tool_filter_json, timeout_seconds, note, created_at, updated_at, "
            "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (server_id, name, transport, command,
             json.dumps(args or []), json.dumps(env or {}),
             url, json.dumps(headers or {}),
             1 if enabled else 0, 1 if is_global else 0,
             1 if trusted_only else 0, json.dumps(tool_filter or {}),
             timeout_seconds, note, now, now, created_by))
        return await self.get(server_id)  # type: ignore[return-value]

    async def update(self, server_id: str,
                     **fields: Any) -> dict[str, Any] | None:
        """Partial update; column whitelist, *_json fields accept decoded
        values. In env/headers a value of "***" keeps the stored one (the
        dashboard redacts on GET — round-tripping must not wipe secrets)."""
        json_columns = {"args", "env", "headers", "tool_filter"}
        scalar = {"name", "transport", "command", "url", "note"}
        bools = {"enabled", "is_global", "trusted_only"}
        if "env" in fields or "headers" in fields:
            stored = await self.get(server_id)
            for key in ("env", "headers"):
                if key in fields and isinstance(fields[key], dict) and stored:
                    fields[key] = self._merge_redacted(
                        fields[key], json.loads(stored[f"{key}_json"] or "{}"))
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key in json_columns:
                sets.append(f"{key}_json = ?")
                params.append(json.dumps(value))
            elif key in scalar:
                sets.append(f"{key} = ?")
                params.append(value)
            elif key in bools:
                sets.append(f"{key} = ?")
                params.append(1 if value else 0)
            elif key == "timeout_seconds":
                if value is None:
                    sets.append("timeout_seconds = NULL")
                else:
                    sets.append("timeout_seconds = ?")
                    params.append(int(value))
        if not sets:
            return await self.get(server_id)
        params.append(_now_iso())
        params.append(server_id)
        await self.db.execute(
            f"UPDATE mcp_servers SET {', '.join(sets)}, updated_at = ? "
            f"WHERE id = ?", tuple(params))
        return await self.get(server_id)

    @staticmethod
    def _merge_redacted(incoming: dict[str, Any],
                        stored: dict[str, Any]) -> dict[str, Any]:
        return {k: stored.get(k, v) if v == "***" else v
                for k, v in incoming.items()}

    async def set_enabled(self, server_id: str, enabled: bool) -> bool:
        changed = await self.db.execute(
            "UPDATE mcp_servers SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now_iso(), server_id))
        return bool(changed)

    async def delete(self, server_id: str) -> bool:
        async with self.db.transaction() as txn:
            await txn.execute(
                "DELETE FROM conversation_mcp_attachments "
                "WHERE mcp_server_id = ?", (server_id,))
            changed = await txn.execute(
                "DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        return bool(changed)

    async def servers_for_conversation(
            self, conversation_id: str) -> list[dict[str, Any]]:
        """The one scoping query the turn path runs: enabled servers that are
        global or attached to this conversation. Trust filtering happens at
        the caller (it needs the session's is_trusted, not SQL)."""
        return [dict(r) for r in await self.db.fetch_all(
            "SELECT * FROM mcp_servers WHERE enabled = 1 AND (is_global = 1 "
            "OR id IN (SELECT mcp_server_id FROM "
            "conversation_mcp_attachments WHERE conversation_id = ?)) "
            "ORDER BY name", (conversation_id,))]

    async def attachments_for(
            self, conversation_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.fetch_all(
            "SELECT a.*, s.name AS server_name FROM "
            "conversation_mcp_attachments a JOIN mcp_servers s "
            "ON s.id = a.mcp_server_id WHERE a.conversation_id = ? "
            "ORDER BY s.name", (conversation_id,))]

    async def set_attachments(self, conversation_id: str,
                              server_ids: list[str],
                              attached_by: str = "") -> None:
        """Idempotent replace: the listed servers and only those."""
        now = _now_iso()
        async with self.db.transaction() as txn:
            await txn.execute(
                "DELETE FROM conversation_mcp_attachments "
                "WHERE conversation_id = ?", (conversation_id,))
            if server_ids:
                for sid in server_ids:
                    await txn.execute(
                        "INSERT OR IGNORE INTO conversation_mcp_attachments "
                        "(conversation_id, mcp_server_id, attached_by, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (conversation_id, sid, attached_by, now))

    async def conversations_for_server(
            self, server_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.fetch_all(
            "SELECT a.conversation_id, a.attached_by, a.created_at FROM "
            "conversation_mcp_attachments a WHERE a.mcp_server_id = ? "
            "ORDER BY a.created_at", (server_id,))]
