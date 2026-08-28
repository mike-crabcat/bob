"""Routines: definition CRUD + firing.

Scheduling rides the unified wakeups mechanism (Bob3): each enabled routine
has one scheduled wakeup row (kind='routine', recurrence='cron:<expr>').
The wakeup pump claims due rows and calls fire_routine(); CRUD here keeps
the wakeup in sync. The legacy RoutineSchedulerTask is gone.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bob_server.services.base import BaseService

logger = logging.getLogger(__name__)

_ROUTINE_COLUMNS = (
    "id, session_key, name, schedule, prompt, enabled, next_run_at, last_run_at, "
    "timezone, valid_from, valid_until, created_at, updated_at"
)


def _routine_tz(row: dict[str, Any]) -> ZoneInfo:
    """Resolve a routine's timezone, falling back to server local."""
    name = row.get("timezone")
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _parse_bound(value: str, tz: ZoneInfo) -> datetime | None:
    """Parse a validity bound into a tz-aware datetime.

    Naive datetimes (including date-only strings) are localized to `tz`. Malformed
    input returns None so callers can treat the bound as open rather than dropping
    the routine silently.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _format_routine_now(row: dict[str, Any]) -> str:
    """Format the current wall-clock time in the routine's timezone.

    Injected into the routine prompt at dispatch time so the LLM has an
    unambiguous local-time anchor. Without it, the model defaults to its
    UTC sense of "today," which is a day behind when the routine's tz is
    far east of UTC (e.g. 07:00 Australia/Perth = 23:00 UTC the prior day).
    The UTC offset is included so the model can convert to other zones itself.
    """
    tz = _routine_tz(row)
    now_local = datetime.now(tz)
    offset = now_local.strftime("%z")  # e.g. +0800
    configured = row.get("timezone")
    tz_label = f"{configured} (UTC{offset})" if configured else f"server local (UTC{offset})"
    return f"[Routine local time: {now_local.strftime('%A %d %B %Y, %H:%M')} {tz_label}]"


def _outside_validity_window(row: dict[str, Any]) -> bool:
    """True if 'now' in the routine's tz falls outside [valid_from, valid_until].

    Bounds are inclusive. Date-only upper bounds extend to end-of-day in the
    routine's tz so the routine still fires on that date.
    """
    tz = _routine_tz(row)
    now_local = datetime.now(tz)

    valid_from = row.get("valid_from")
    if valid_from:
        bound = _parse_bound(valid_from, tz)
        if bound is not None and now_local < bound:
            return True

    valid_until = row.get("valid_until")
    if valid_until:
        bound = _parse_bound(valid_until, tz)
        if bound is not None:
            if "T" not in valid_until:
                bound = bound + timedelta(days=1)
            if now_local >= bound:
                return True

    return False


class RoutineService(BaseService):
    async def list_routines(self, session_key: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines WHERE session_key = ? ORDER BY name",
            (session_key,),
        )
        return [dict(r) for r in rows] if rows else []

    async def get_routine(self, session_key: str, name: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines WHERE session_key = ? AND name = ?",
            (session_key, name),
        )
        return dict(row) if row else None

    async def upsert_routine(
        self,
        session_key: str,
        name: str,
        schedule: str,
        prompt: str,
        enabled: bool = True,
        next_run_at: str | None = None,
        *,
        timezone: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        existing = await self.get_routine(session_key, name)
        now = datetime.now().astimezone().isoformat()

        if existing:
            await self.db.execute(
                "UPDATE routines SET schedule = ?, prompt = ?, enabled = ?, next_run_at = ?, "
                "timezone = ?, valid_from = ?, valid_until = ?, updated_at = ? "
                "WHERE session_key = ? AND name = ?",
                (schedule, prompt, int(enabled), next_run_at, timezone, valid_from, valid_until, now, session_key, name),
            )
        else:
            await self.db.execute(
                "INSERT INTO routines (id, session_key, name, schedule, prompt, enabled, next_run_at, "
                "timezone, valid_from, valid_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), session_key, name, schedule, prompt, int(enabled), next_run_at,
                 timezone, valid_from, valid_until, now, now),
            )

        result = await self.get_routine(session_key, name)
        assert result is not None
        await self._sync_wakeup(result)
        return result

    async def _sync_wakeup(self, routine: dict[str, Any]) -> None:
        """Keep exactly one scheduled routine-wakeup per enabled routine."""
        from bob_server.repositories.wakeups import WakeupRepository

        repo = WakeupRepository(self.db)
        await repo.cancel_for_routine(routine["id"])
        if routine["enabled"] and routine.get("next_run_at"):
            await repo.schedule(
                conversation_id=routine["session_key"],
                not_before=routine["next_run_at"],
                recurrence=f"cron:{routine['schedule']}",
                tz=routine.get("timezone"),
                kind="routine",
                payload={"routine_id": routine["id"]},
            )

    async def delete_routine(self, session_key: str, name: str) -> bool:
        existing = await self.get_routine(session_key, name)
        count = await self.db.execute(
            "DELETE FROM routines WHERE session_key = ? AND name = ?",
            (session_key, name),
        )
        if existing:
            from bob_server.repositories.wakeups import WakeupRepository
            await WakeupRepository(self.db).cancel_for_routine(existing["id"])
        return count > 0

    async def get_by_id(self, routine_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines WHERE id = ?", (routine_id,)
        )
        return dict(row) if row else None

    async def mark_run(self, routine_id: str) -> None:
        """Record last_run_at on a routine already claimed via claim()."""
        now = datetime.now().astimezone().isoformat()
        await self.db.execute(
            "UPDATE routines SET last_run_at = ?, updated_at = ? WHERE id = ?",
            (now, now, routine_id),
        )


async def append_fired_event(ctx: Any, routine: dict[str, Any], slot: str) -> None:
    """Append routine.fired (audit-only). Idempotent per routine id + run slot,
    so a crashed-and-replayed claim can't double-append. Best-effort — an
    append failure must never block the routine."""
    try:
        from bob_server.repositories import Event, EventLogRepository

        session_key = routine["session_key"]
        await EventLogRepository(ctx.db).append(Event(
            event_type="routine.fired",
            binding_key=session_key,
            conversation_id=session_key,
            source="routine",
            external_id=f"{routine['id']}:{slot}",
            payload={"routine_id": routine["id"], "name": routine.get("name"),
                     "slot": slot},
        ))
    except Exception:
        logger.warning("routine.fired event append failed for %s",
                       routine.get("id"), exc_info=True)


def fire_routine_detached(ctx: Any, routine: dict[str, Any]) -> None:
    """Run a routine dispatch without blocking the caller (the wakeup pump)."""
    asyncio.create_task(fire_routine(ctx, routine))


async def fire_routine(ctx: Any, routine: dict[str, Any]) -> None:
    """Execute one routine run: self-contained prompt → LLM turn with delivery
    tools. Moved from the deleted RoutineSchedulerTask."""
    from uuid import uuid4

    from bob_server.services.dispatch_runner import is_no_reply, resolve_session_model
    from bob_server.services.llm_dispatch import LLMDispatchService
    from bob_server.services.prompt_assembler import (
        build_chat_messages,
        load_workspace_prompt,
    )
    from bob_server.services.session_service import SessionService
    from bob_server.services.tool_registry import build_common_tools
    from bob_server.services.tools import Tool
    from bob_server.services.wake_service import session_key_to_chat_id

    session_key = routine["session_key"]
    prompt = routine["prompt"]
    name = routine["name"]

    try:
        prompt = f"{_format_routine_now(routine)}\n\n{prompt}"

        session_svc = SessionService(ctx)
        await session_svc.add_message(session_key, "user", prompt, channel="routine", provenance="routine")

        settings = ctx.settings
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=ctx.db)

        # Resolve session trust level for correct tool set
        from bob_server.repositories.conversations import ConversationRepository
        route = await ConversationRepository(ctx.db).route_for(session_key)
        is_trusted = False
        contact_id = route["contact_id"] if route else None
        if route and contact_id:
            from bob_server.repositories.contacts import ContactRepository
            trusted = await ContactRepository(ctx.db).is_trusted(contact_id)
            if trusted is not None:
                is_trusted = trusted

        # Routines carry their own self-contained prompt — skip session history
        # (which includes the original "set up this routine" conversation)
        messages = await build_chat_messages(
            prompt, "",
            system_content=workspace_prompt,
        )
        tools = build_common_tools(
            ctx, session_key=session_key, is_trusted=is_trusted,
            contact_id=contact_id, include_routines=False,
        )

        # Add channel-specific delivery tools
        wa_bridge = ctx.whatsapp_bridge
        chat_id = session_key_to_chat_id(session_key)
        if chat_id and wa_bridge and wa_bridge.connected:
            async def _send_whatsapp_message(text: str) -> str:
                if is_no_reply(text):
                    return "No reply sent."
                request_id = await wa_bridge.send_message(chat_id, text)
                return f"Message sent (request_id={request_id})"

            tools.append(Tool(
                name="send_whatsapp_message",
                description=(
                    "Send a reply to the current WhatsApp conversation. "
                    "You MUST call this tool to deliver your response — your text output will NOT be sent."
                ),
                parameters={
                    "text": {"type": "string", "description": "The message text to send."},
                },
                required=["text"],
                handler=_send_whatsapp_message,
            ))

        dispatch_id = str(uuid4())
        # Routine replies are user-visible conversation turns — they follow the
        # session's /model override, same as main dispatch turns.
        model_arg = await resolve_session_model(ctx.db, ctx.settings, session_key)
        response = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools,
            model=model_arg,
            call_category="routine",
            session_key=session_key,
            dispatch_id=dispatch_id,
        )

        await session_svc.add_message(session_key, "assistant", response, channel="routine", dispatch_id=dispatch_id, provenance="routine")

        await RoutineService(ctx).mark_run(routine["id"])

        logger.info("Routine '%s' fired for session %s", name, session_key)
    except Exception:
        logger.exception("Routine '%s' failed for session %s", name, session_key)
