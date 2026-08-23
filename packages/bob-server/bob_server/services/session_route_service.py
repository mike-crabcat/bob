"""Session route registry (legacy, write-only).

Routes dual-write into bindings (Increment 4); ConversationRepository.route_for
is the read path. The resolver family was deleted — it had no callers. This
service survives only until the CRUD surfaces (REST/CLI) are retired and the
table is dropped."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from bob_server.config import Settings
from bob_server.context import AppContext
from bob_server.database import Database
from bob_server.exceptions import ConflictError, NotFoundError
from bob_server.models import (
    SessionRouteCreate,
    SessionRouteKind,
    SessionRouteResponse,
    SessionRouteUpdate,
)
from bob_server.services.base import BaseService, json_dumps, json_loads, utcnow


class SessionRouteService(BaseService):
    """Persist and resolve channel/session routing for outbound delivery."""

    logger = logging.getLogger(__name__)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)

    @property
    def settings(self) -> Settings:
        return self._get_settings()

    async def _mirror_binding(self, route_id: str) -> None:
        """Increment 4 dual-write: keep the binding row in step with the
        route so route_for() can replace every session_routes read site."""
        try:
            row = await self.db.fetch_one(
                "SELECT * FROM session_routes WHERE id = ?", (route_id,))
            if row is None:
                return
            from bob_server.repositories.conversations import ConversationRepository

            repo = ConversationRepository(self.db)
            kind = row["kind"]
            address = row["chat_id"]
            if not address and row["contact_id"]:
                c = await self.db.fetch_one(
                    "SELECT phone_number, email FROM contacts WHERE id = ?",
                    (row["contact_id"],))
                if c:
                    address = c["phone_number"] or c["email"]
            await repo.ensure(row["session_key"], address=address, endpoint_kind=kind)
            await self.db.execute(
                """UPDATE bindings SET
                       contact_id = COALESCE(?, contact_id),
                       is_active = ?
                   WHERE session_key = ?""",
                (row["contact_id"],
                 1 if (row["is_active"] and row["deleted_at"] is None) else 0,
                 row["session_key"]))
        except Exception:
            self.logger.warning("binding mirror failed for route %s", route_id, exc_info=True)

    async def create_route(self, payload: SessionRouteCreate) -> SessionRouteResponse:
        now = utcnow().isoformat()
        await self._validate_payload(payload.channel, payload.kind, payload.chat_id, payload.contact_id)
        existing = await self.db.fetch_one(
            "SELECT * FROM session_routes WHERE channel = ? AND session_key = ?",
            (payload.channel, payload.session_key),
        )
        if existing is not None:
            if existing.get("deleted_at") is None:
                raise ConflictError(
                    f"Session route already exists for {payload.channel}:{payload.session_key}. Update it instead."
                )
            await self.db.execute(
                """
                UPDATE session_routes
                SET kind = ?, chat_id = ?, contact_id = ?, metadata = ?, is_active = 1, updated_at = ?, deleted_at = NULL
                WHERE id = ?
                """,
                (
                    payload.kind.value,
                    payload.chat_id,
                    str(payload.contact_id) if payload.contact_id else None,
                    json_dumps(payload.metadata),
                    now,
                    existing["id"],
                ),
            )
            await self._mirror_binding(existing["id"])
            return await self.get_route(existing["id"])

        route_id = str(uuid4())
        await self.db.execute(
            """
            INSERT INTO session_routes (
                id, channel, session_key, kind, chat_id, contact_id, metadata, is_active,
                created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
            """,
            (
                route_id,
                payload.channel,
                payload.session_key,
                payload.kind.value,
                payload.chat_id,
                str(payload.contact_id) if payload.contact_id else None,
                json_dumps(payload.metadata),
                now,
                now,
            ),
        )
        await self._mirror_binding(route_id)
        return await self.get_route(route_id)

    async def list_routes(
        self,
        *,
        channel: str | None = None,
        active_only: bool = True,
    ) -> list[SessionRouteResponse]:
        query = "SELECT * FROM session_routes WHERE deleted_at IS NULL"
        params: list[Any] = []
        if channel is not None:
            query += " AND channel = ?"
            params.append(channel)
        if active_only:
            query += " AND is_active = 1"
        query += " ORDER BY channel ASC, session_key ASC"
        rows = await self.db.fetch_all(query, tuple(params))
        return [SessionRouteResponse.model_validate(self._decode_route_row(row)) for row in rows]

    async def get_route(self, route_id: str) -> SessionRouteResponse:
        row = await self.db.fetch_one(
            "SELECT * FROM session_routes WHERE id = ? AND deleted_at IS NULL",
            (route_id,),
        )
        if row is None:
            raise NotFoundError(f"Session route '{route_id}' was not found")
        return SessionRouteResponse.model_validate(self._decode_route_row(row))

    async def update_route(self, route_id: str, payload: SessionRouteUpdate) -> SessionRouteResponse:
        existing = await self.db.fetch_one(
            "SELECT * FROM session_routes WHERE id = ? AND deleted_at IS NULL",
            (route_id,),
        )
        if existing is None:
            raise NotFoundError(f"Session route '{route_id}' was not found")

        values = payload.model_dump(exclude_unset=True, mode="json")
        if not values:
            return SessionRouteResponse.model_validate(self._decode_route_row(existing))

        merged_chat_id = values.get("chat_id", existing.get("chat_id"))
        merged_contact_id = values.get("contact_id", existing.get("contact_id"))
        await self._validate_payload(existing["channel"], SessionRouteKind(existing["kind"]), merged_chat_id, merged_contact_id)

        if "contact_id" in values and values["contact_id"] is not None:
            values["contact_id"] = str(values["contact_id"])
        if "metadata" in values and values["metadata"] is not None:
            values["metadata"] = json_dumps(values["metadata"])
        if "is_active" in values:
            values["is_active"] = 1 if values["is_active"] else 0
        values["updated_at"] = utcnow().isoformat()

        assignments = ", ".join(f"{field} = ?" for field in values)
        await self.db.execute(
            f"UPDATE session_routes SET {assignments} WHERE id = ? AND deleted_at IS NULL",
            tuple(values.values()) + (route_id,),
        )
        await self._mirror_binding(route_id)
        return await self.get_route(route_id)

    async def delete_route(self, route_id: str) -> None:
        await self.get_route(route_id)
        now = utcnow().isoformat()
        await self.db.execute(
            "UPDATE session_routes SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, now, route_id),
        )
        await self._mirror_binding(route_id)

    async def _validate_payload(
        self,
        channel: str,
        kind: SessionRouteKind,
        chat_id: str | None,
        contact_id: str | Any | None,
    ) -> None:
        if channel == "whatsapp":
            if kind == SessionRouteKind.GROUP:
                if not chat_id:
                    raise ConflictError("Group session routes require chat_id")
                return
            if contact_id is None:
                raise ConflictError("DM session routes require contact_id")
            from bob_server.repositories.contacts import ContactRepository
            row = await ContactRepository(self.db).get(str(contact_id))
            if row is None:
                raise NotFoundError(f"Contact '{contact_id}' was not found")
            if not (row.get("phone_number") or "").strip():
                raise ConflictError(f"Contact '{contact_id}' does not have a usable phone number")
            return
        if channel == "email":
            if kind == SessionRouteKind.THREAD:
                if not chat_id:
                    raise ConflictError("Thread email session routes require chat_id (thread_id)")
                return
            raise ConflictError("Email session routes must use kind 'thread'")
        if channel == "phone":
            if kind == SessionRouteKind.DM:
                if contact_id is None:
                    raise ConflictError("Phone DM session routes require contact_id")
                return
            raise ConflictError("Phone session routes must use kind 'dm'")
        raise ConflictError(f"Unsupported channel: {channel}")

    def _decode_route_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        decoded["kind"] = SessionRouteKind(decoded["kind"])
        decoded["metadata"] = json_loads(decoded.get("metadata"), {})
        decoded["is_active"] = bool(decoded.get("is_active", 0))
        return decoded
