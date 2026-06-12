"""Session route registry and delivery route resolution helpers."""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from bob_server.config import Settings
from bob_server.context import AppContext
from bob_server.database import Database
from bob_server.exceptions import ConflictError, NotFoundError
from bob_server.models import (
    ResolvedSessionRoute,
    SessionRouteCreate,
    SessionRouteKind,
    SessionRouteResponse,
    SessionRouteUpdate,
)
from bob_server.services.base import BaseService, json_dumps, json_loads, utcnow


SOURCE_ROUTE_FIELDS = ("channel", "session_key", "chat_id")


def extract_source_route_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    if not isinstance(metadata, dict):
        return extracted
    for field in SOURCE_ROUTE_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            extracted[field] = value.strip()
    return extracted


def has_source_route_metadata(metadata: dict[str, Any] | None) -> bool:
    extracted = extract_source_route_metadata(metadata)
    channel = extracted.get("channel")
    if channel == "whatsapp":
        return bool(extracted.get("chat_id") or extracted.get("session_key"))
    if channel == "email":
        return bool(extracted.get("chat_id") or extracted.get("session_key"))
    return False


def merge_source_route_metadata(
    metadata: dict[str, Any] | None,
    inherited_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    inherited = extract_source_route_metadata(inherited_metadata)
    for field, value in inherited.items():
        existing = merged.get(field)
        if not isinstance(existing, str) or not existing.strip():
            merged[field] = value
    return merged


class SessionRouteService(BaseService):
    """Persist and resolve channel/session routing for outbound delivery."""

    logger = logging.getLogger(__name__)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)

    @property
    def settings(self) -> Settings:
        return self._get_settings()

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
        return await self.get_route(route_id)

    async def delete_route(self, route_id: str) -> None:
        await self.get_route(route_id)
        now = utcnow().isoformat()
        await self.db.execute(
            "UPDATE session_routes SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, now, route_id),
        )

    async def resolve_registered_route(self, channel: str, session_key: str) -> ResolvedSessionRoute | None:
        row = await self.db.fetch_one(
            """
            SELECT *
            FROM session_routes
            WHERE channel = ? AND session_key = ? AND deleted_at IS NULL AND is_active = 1
            """,
            (channel, session_key),
        )
        if row is None:
            return None
        return await self._resolve_route_row(row)

    async def resolve_registered_group_route_by_chat_id(self, channel: str, chat_id: str) -> ResolvedSessionRoute | None:
        row = await self.db.fetch_one(
            """
            SELECT *
            FROM session_routes
            WHERE channel = ? AND kind = ? AND chat_id = ? AND deleted_at IS NULL AND is_active = 1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (channel, SessionRouteKind.GROUP.value, chat_id),
        )
        if row is None:
            return None
        return await self._resolve_route_row(row)

    async def resolve_registered_dm_route_by_contact_id(self, channel: str, contact_id: str) -> ResolvedSessionRoute | None:
        row = await self.db.fetch_one(
            """
            SELECT *
            FROM session_routes
            WHERE channel = ? AND kind = ? AND contact_id = ? AND deleted_at IS NULL AND is_active = 1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (channel, SessionRouteKind.DM.value, contact_id),
        )
        if row is None:
            return None
        return await self._resolve_route_row(row)

    async def resolve_source_metadata(self, metadata: dict[str, Any]) -> ResolvedSessionRoute | None:
        channel = metadata.get("channel")
        if channel == "email":
            return await self._resolve_email_source_metadata(metadata)
        if channel != "whatsapp":
            return None
        chat_id = metadata.get("chat_id")
        session_key = metadata.get("session_key")
        if isinstance(chat_id, str) and chat_id.strip():
            normalized_chat_id = chat_id.strip()
            if normalized_chat_id.endswith("@g.us"):
                # Group chat
                registered = await self.resolve_registered_group_route_by_chat_id("whatsapp", normalized_chat_id)
                return ResolvedSessionRoute(
                    channel="whatsapp",
                    kind=SessionRouteKind.GROUP,
                    to=normalized_chat_id,
                    session_key=session_key or (registered.session_key if registered is not None else None),
                    chat_id=normalized_chat_id,
                    route_source="metadata.chat_id",
                )
            else:
                # Direct message — phone number chat_id, derive session key
                derived_session_key = session_key or self._build_whatsapp_dm_session_key(normalized_chat_id)
                return ResolvedSessionRoute(
                    channel="whatsapp",
                    kind=SessionRouteKind.DM,
                    to=normalized_chat_id,
                    session_key=derived_session_key,
                    phone_number=normalized_chat_id,
                    route_source="metadata.chat_id",
                )
        if isinstance(session_key, str) and session_key.strip():
            normalized_session_key = session_key.strip()
            resolved = await self.resolve_registered_route("whatsapp", normalized_session_key)
            if resolved is not None:
                return resolved
            derived_phone_number = self._derive_whatsapp_direct_recipient_from_session_key(normalized_session_key)
            if derived_phone_number is not None:
                return ResolvedSessionRoute(
                    channel="whatsapp",
                    kind=SessionRouteKind.DM,
                    to=derived_phone_number,
                    session_key=normalized_session_key,
                    phone_number=derived_phone_number,
                    route_source="metadata.session_key",
                )
            derived_chat_id = self._derive_whatsapp_group_chat_id_from_session_key(normalized_session_key)
            if derived_chat_id is not None:
                return ResolvedSessionRoute(
                    channel="whatsapp",
                    kind=SessionRouteKind.GROUP,
                    to=derived_chat_id,
                    session_key=normalized_session_key,
                    chat_id=derived_chat_id,
                    route_source="metadata.session_key",
                )
        return None

    async def resolve_target_metadata(self, metadata: dict[str, Any]) -> ResolvedSessionRoute | None:
        target_session = metadata.get("target_session")
        if not isinstance(target_session, dict):
            return None

        channel = target_session.get("channel")
        kind = target_session.get("kind")

        # Non-WhatsApp target session with only a session_key
        # (e.g., bob:project:X:task:Y — no channel/kind set)
        if channel is None and kind is None:
            session_key = target_session.get("session_key")
            if isinstance(session_key, str) and session_key.strip():
                # Resolve channel/to from source metadata for delivery routing
                source_route = await self.resolve_source_metadata(metadata)
                if source_route is not None:
                    return ResolvedSessionRoute(
                        channel=source_route.channel,
                        kind=source_route.kind,
                        to=source_route.to,
                        session_key=session_key.strip(),
                        route_source="target_session.session_key",
                    )
                # Fallback: try related source routes from project
                related_route = await self._resolve_related_source_route(metadata)
                if related_route is not None:
                    return ResolvedSessionRoute(
                        channel=related_route.channel,
                        kind=related_route.kind,
                        to=related_route.to,
                        session_key=session_key.strip(),
                        route_source="target_session.session_key",
                    )
                return ResolvedSessionRoute(
                    session_key=session_key.strip(),
                    route_source="target_session.session_key_only",
                )

        if channel == "email":
            return await self._resolve_email_target_metadata(target_session, metadata)

        if channel != "whatsapp":
            return None

        if kind == SessionRouteKind.GROUP.value:
            chat_id = target_session.get("chat_id")
            session_key = target_session.get("session_key")
            if isinstance(chat_id, str) and chat_id.strip():
                registered = await self.resolve_registered_group_route_by_chat_id("whatsapp", chat_id.strip())
                return ResolvedSessionRoute(
                    channel="whatsapp",
                    kind=SessionRouteKind.GROUP,
                    to=chat_id.strip(),
                    session_key=session_key or (registered.session_key if registered is not None else None),
                    chat_id=chat_id.strip(),
                    route_source="target_session.chat_id",
                )
            if isinstance(session_key, str) and session_key.strip():
                return await self.resolve_registered_route("whatsapp", session_key.strip())
            return None

        if kind == SessionRouteKind.DM.value:
            contact_id = target_session.get("contact_id")
            if contact_id is None:
                return None
            registered = await self.resolve_registered_dm_route_by_contact_id("whatsapp", str(contact_id))
            return await self._resolve_contact_route(
                str(contact_id),
                session_key=registered.session_key if registered is not None else None,
                source_session_key=metadata.get("session_key"),
                route_source="target_session.contact_id",
            )
        return None

    async def resolve_notification_route(self, metadata: dict[str, Any]) -> ResolvedSessionRoute | None:
        if metadata.get("delivery_route") == "target":
            return await self.resolve_target_metadata(metadata)

        route = await self.resolve_source_metadata(metadata)
        if route is not None:
            return route

        related_route = await self._resolve_related_source_route(metadata)
        if related_route is not None:
            return related_route

        # Fallback to default contact from database
        default_contact = await self.db.fetch_one(
            "SELECT id FROM contacts WHERE is_default = 1 AND deleted_at IS NULL LIMIT 1"
        )
        if default_contact:
            self.logger.info(
                f"No route found in metadata, using default contact fallback: {default_contact['id']}"
            )
            return await self._resolve_contact_route(
                default_contact["id"],
                session_key=None,
                route_source="default_contact",
            )

        self.logger.warning(
            f"No notification route could be resolved from metadata: {metadata.get('delivery_route')}"
        )
        return None

    async def _resolve_related_source_route(self, metadata: dict[str, Any]) -> ResolvedSessionRoute | None:
        candidate_project_ids: list[str] = []
        for key in ("parent_project_id", "project_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip() and value not in candidate_project_ids:
                candidate_project_ids.append(value)

        for project_id in candidate_project_ids:
            project = await self.db.fetch_one(
                "SELECT metadata FROM projects WHERE id = ? AND deleted_at IS NULL",
                (project_id,),
            )
            if project is None:
                continue
            route = await self.resolve_source_metadata(json_loads(project.get("metadata"), {}))
            if route is not None:
                return route

        task_id = metadata.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return None

        task = await self.db.fetch_one(
            "SELECT metadata FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,),
        )
        if task is not None:
            route = await self.resolve_source_metadata(json_loads(task.get("metadata"), {}))
            if route is not None:
                return route

        linked_projects = await self.db.fetch_all(
            """
            SELECT p.metadata
            FROM project_tasks AS pt
            INNER JOIN projects AS p ON p.id = pt.project_id
            WHERE pt.task_id = ? AND p.deleted_at IS NULL
            ORDER BY p.created_at DESC
            """,
            (task_id,),
        )
        for project in linked_projects:
            route = await self.resolve_source_metadata(json_loads(project.get("metadata"), {}))
            if route is not None:
                return route
        return None

    async def resolve_target_session_key(self, metadata: dict[str, Any]) -> str | None:
        target_session = metadata.get("target_session")
        if not isinstance(target_session, dict):
            return None

        # Non-WhatsApp target session with only a session_key
        # (e.g., bob:project:X:task:Y — no channel/kind set)
        if target_session.get("channel") is None and target_session.get("kind") is None:
            session_key = target_session.get("session_key")
            if isinstance(session_key, str) and session_key.strip():
                return session_key.strip()

        if target_session.get("channel") != "whatsapp":
            return None

        kind = target_session.get("kind")
        if kind == SessionRouteKind.GROUP.value:
            session_key = target_session.get("session_key")
            if isinstance(session_key, str) and session_key.strip():
                return session_key.strip()

            chat_id = target_session.get("chat_id")
            if not isinstance(chat_id, str) or not chat_id.strip():
                return None
            registered = await self.resolve_registered_group_route_by_chat_id("whatsapp", chat_id.strip())
            return registered.session_key if registered is not None else None

        if kind == SessionRouteKind.DM.value:
            contact_id = target_session.get("contact_id")
            if contact_id is None:
                return None
            registered = await self.resolve_registered_dm_route_by_contact_id("whatsapp", str(contact_id))
            if registered is not None:
                return registered.session_key
            resolved = await self._resolve_contact_route(
                str(contact_id),
                source_session_key=metadata.get("session_key"),
                route_source="target_session.contact_id",
            )
            return resolved.session_key

        return None

    async def _resolve_route_row(self, row: dict[str, Any]) -> ResolvedSessionRoute:
        metadata = json_loads(row.get("metadata"), {})
        if row["kind"] == SessionRouteKind.GROUP.value:
            return ResolvedSessionRoute(
                channel=row["channel"],
                kind=SessionRouteKind.GROUP,
                to=row["chat_id"],
                session_key=row["session_key"],
                chat_id=row["chat_id"],
                route_source="session_routes",
                metadata=metadata,
            )
        if row["kind"] == SessionRouteKind.THREAD.value:
            return ResolvedSessionRoute(
                channel=row["channel"],
                kind=SessionRouteKind.THREAD,
                to=row["chat_id"],
                session_key=row["session_key"],
                chat_id=row["chat_id"],
                route_source="session_routes",
                metadata=metadata,
            )
        return await self._resolve_contact_route(row["contact_id"], session_key=row["session_key"], metadata=metadata, route_source="session_routes")

    async def _resolve_email_source_metadata(self, metadata: dict[str, Any]) -> ResolvedSessionRoute | None:
        chat_id = metadata.get("chat_id")
        session_key = metadata.get("session_key")
        if isinstance(chat_id, str) and chat_id.strip():
            thread_id = chat_id.strip()
            registered = await self.resolve_registered_route("email", thread_id)
            derived_session_key = session_key or (registered.session_key if registered else None) or self._build_email_thread_session_key(thread_id)
            return ResolvedSessionRoute(
                channel="email",
                kind=SessionRouteKind.THREAD,
                to=thread_id,
                session_key=derived_session_key,
                chat_id=thread_id,
                route_source="metadata.chat_id",
            )
        if isinstance(session_key, str) and session_key.strip():
            resolved = await self.resolve_registered_route("email", session_key.strip())
            if resolved is not None:
                return resolved
        return None

    async def _resolve_email_target_metadata(self, target_session: dict[str, Any], metadata: dict[str, Any]) -> ResolvedSessionRoute | None:
        kind = target_session.get("kind")
        if kind == SessionRouteKind.THREAD.value or kind is None:
            session_key = target_session.get("session_key")
            chat_id = target_session.get("chat_id")
            if isinstance(chat_id, str) and chat_id.strip():
                thread_id = chat_id.strip()
                registered = await self.resolve_registered_route("email", thread_id)
                derived_session_key = session_key or (registered.session_key if registered else None) or self._build_email_thread_session_key(thread_id)
                return ResolvedSessionRoute(
                    channel="email",
                    kind=SessionRouteKind.THREAD,
                    to=thread_id,
                    session_key=derived_session_key,
                    chat_id=thread_id,
                    route_source="target_session.chat_id",
                )
            if isinstance(session_key, str) and session_key.strip():
                return await self.resolve_registered_route("email", session_key.strip())
        return None

    async def _resolve_contact_route(
        self,
        contact_id: str,
        *,
        session_key: str | None = None,
        source_session_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        route_source: str,
    ) -> ResolvedSessionRoute:
        row = await self.db.fetch_one(
            """
            SELECT id, name, phone_number
            FROM contacts
            WHERE id = ? AND deleted_at IS NULL
            """,
            (contact_id,),
        )
        if row is None:
            raise NotFoundError(f"Contact '{contact_id}' was not found")
        phone_number = (row.get("phone_number") or "").strip()
        if not phone_number:
            raise ConflictError(f"Contact '{contact_id}' does not have a usable phone number")
        resolved_session_key = session_key or self._build_whatsapp_dm_session_key(
            phone_number,
            source_session_key=source_session_key,
        )
        return ResolvedSessionRoute(
            channel="whatsapp",
            kind=SessionRouteKind.DM,
            to=phone_number,
            session_key=resolved_session_key,
            contact_id=row["id"],
            contact_name=row["name"],
            phone_number=phone_number,
            route_source=route_source,
            metadata=metadata or {},
        )

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
            await self._resolve_contact_route(str(contact_id), route_source="session_routes.validation")
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

    def _build_whatsapp_dm_session_key(self, phone_number: str, *, source_session_key: str | None = None) -> str:
        return f"{self._resolve_whatsapp_agent_prefix(source_session_key)}whatsapp:direct:{phone_number}"

    def _build_email_thread_session_key(self, thread_id: str) -> str:
        return f"{self._resolve_agent_prefix()}email:thread:{thread_id}"

    def _resolve_agent_prefix(self) -> str:
        return "agent:main:"

    def _resolve_whatsapp_agent_prefix(self, source_session_key: str | None) -> str:
        if isinstance(source_session_key, str):
            match = re.match(r"^(?P<prefix>agent:[^:]+:)whatsapp:(?:direct|group):", source_session_key.strip())
            if match is not None:
                return match.group("prefix")

        return self._resolve_agent_prefix()

    def _derive_whatsapp_group_chat_id_from_session_key(self, session_key: str) -> str | None:
        match = re.search(r"(?:^|:)whatsapp:group:(?P<chat_id>[^:]+@g\.us)$", session_key)
        if match is None:
            return None
        return match.group("chat_id")

    def _derive_whatsapp_direct_recipient_from_session_key(self, session_key: str) -> str | None:
        match = re.search(r"(?:^|:)whatsapp:direct:(?P<phone_number>\+?[0-9]+)$", session_key)
        if match is None:
            return None
        return match.group("phone_number")
