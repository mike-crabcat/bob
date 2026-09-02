"""Out-of-channel answer detector for outreach goals.

Outreach goals complete inside their target DM — but people answer in the
wrong channel. The David/coffee incident (2026-08-25): David confirmed in
the shared group two minutes after the outreach DM went out; the DM agent
never saw it, the confirmation was never extracted as a claim, and the goal
sat active until manually cancelled. This sweep watches inbound
``message.received`` events; when the sender has an active outreach goal
working a DIFFERENT conversation, a cheap LLM probe asks whether the
message satisfies that objective. A satisfied verdict settles the goal
through ``complete_goal`` — the single chokepoint, so parent roll-up and
the wake matrix behave exactly as an in-channel completion would.

Idempotency: one probe per (goal, message) — ``outreach_probe_log`` UNIQUE.
Kill switch: ``BOB_OUTREACH_DETECTOR_DISABLED`` (watermark frozen while
disabled, so lifting it replays the gap — same contract as the claim
router). First run positions the watermark at the newest existing event:
the detector addresses future messages, not history.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from bob_server.context import AppContext

logger = logging.getLogger(__name__)

DETECTED_EVENT_TYPE = "message.received"
_MAX_EVENTS_PER_TICK = 20

_PROBE_SYSTEM = """You decide whether a message from a person satisfies an outreach \
objective that was put to them elsewhere (they may be answering in the wrong \
channel — e.g. a shared group instead of the private chat the question went to). \
Answer satisfied=true ONLY if the message, on its own, confirms or resolves the \
objective: an explicit acceptance, a decline, or the specific information the \
objective asked for. Small talk, unrelated topics, and ambiguous replies are not \
satisfied. When unsure, answer false — a missed completion is recoverable, a \
wrong one is not. Respond with ONLY a JSON object: \
{"satisfied": true|false, "note": "<one short sentence citing the words that decide it>"}"""


def detector_disabled() -> bool:
    return os.getenv("BOB_OUTREACH_DETECTOR_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------- watermark

async def get_watermark(db: Any) -> str | None:
    row = await db.fetch_one(
        "SELECT event_id FROM outreach_detector_watermark WHERE id = 1")
    return row["event_id"] if row else None


async def advance_watermark(db: Any, event_id: str) -> None:
    from bob_server.services.base import utcnow
    await db.execute(
        """INSERT INTO outreach_detector_watermark (id, event_id, updated_at)
           VALUES (1, ?, ?)
           ON CONFLICT (id) DO UPDATE SET event_id = excluded.event_id,
                                          updated_at = excluded.updated_at""",
        (event_id, utcnow().isoformat()),
    )


async def _newest_event_id(db: Any) -> str:
    from bob_server.repositories.event_log import EventLogRepository

    return await EventLogRepository(db).newest_event_id(DETECTED_EVENT_TYPE)


# ------------------------------------------------------------------- sweep

async def sweep(ctx: AppContext, *, limit: int = _MAX_EVENTS_PER_TICK) -> int:
    """Probe un-seen inbound messages against their senders' outreach goals.

    Returns the number of events processed (probed or skipped)."""
    if detector_disabled():
        return 0

    wm = await get_watermark(ctx.db)
    if wm is None:
        # First run: start at the newest event — no historical back-probing.
        await advance_watermark(ctx.db, await _newest_event_id(ctx.db))
        return 0

    from bob_server.repositories.event_log import EventLogRepository

    rows = await EventLogRepository(ctx.db).events_after(
        DETECTED_EVENT_TYPE, wm, limit=limit)
    for ev in rows or []:
        try:
            await _process_event(ctx, ev)
        except Exception:
            logger.exception("outreach detector failed for event %s", ev["id"])
        # Advance per event: a failure never blocks the queue; the probe log's
        # unique key makes a reprocessed event harmless (one wasted probe at
        # worst if we crashed between probing and logging).
        await advance_watermark(ctx.db, ev["id"])
    return len(rows or [])


async def _process_event(ctx: AppContext, ev: dict[str, Any]) -> None:
    payload = json.loads(ev.get("payload_json") or "{}")
    contact_id = str(payload.get("contact_id") or "")
    message_id = str(payload.get("session_message_id") or "")
    if not contact_id or not message_id:
        return
    from bob_server.repositories.history import HistoryRepository

    message = await HistoryRepository(ctx.db).message_by_id(message_id)
    if message is None or message["synthetic"]:
        return
    text = (message["content"] or "").strip()
    if not text:
        return  # media-only

    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.repositories.goals import GoalRepository
    from bob_server.repositories.participants import ParticipantRepository

    goal_convs = await ParticipantRepository(ctx.db).conversations_for_contact(contact_id)
    # Event rows carry raw session_keys; goals/participants hold canonical ids.
    event_cid = await ConversationRepository(ctx.db).resolve_cid(
        ev.get("conversation_id") or "")
    repo = GoalRepository(ctx.db)
    for conv in goal_convs:
        if conv == event_cid:
            continue  # in-channel: the goal's own conversation handles it
        for goal in await repo.list_active(conversation_id=conv, limit=20):
            if goal["kind"] != "outreach":
                continue
            await _probe_goal(ctx, goal=goal, text=text, message_id=message_id,
                              sender_name=str(payload.get("sender_name") or ""))


async def _probe_goal(ctx: AppContext, *, goal: dict[str, Any], text: str,
                      message_id: str, sender_name: str) -> None:
    seen = await ctx.db.fetch_one(
        "SELECT 1 FROM outreach_probe_log WHERE goal_id = ? AND message_id = ?",
        (goal["id"], message_id))
    if seen:
        return

    verdict, note = await _run_probe(ctx, goal=goal, text=text, sender_name=sender_name)
    await ctx.db.execute(
        """INSERT OR IGNORE INTO outreach_probe_log (id, goal_id, message_id, verdict, note, created_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (f"oprobe-{uuid.uuid4().hex[:12]}", goal["id"], message_id, verdict, note[:500]))

    if verdict != "satisfied":
        if verdict == "error":
            logger.warning("outreach probe errored for goal %s", goal["id"])
        return

    from bob_server.services.goal_service import complete_goal

    who = sender_name or "the contact"
    result = f"Out-of-channel confirmation from {who}: {note or text[:200]}"
    ok = await complete_goal(ctx, goal["id"], result=result)
    if ok:
        logger.info("outreach detector completed goal %s off a message in another conversation",
                    goal["id"])


async def _run_probe(ctx: AppContext, *, goal: dict[str, Any], text: str,
                     sender_name: str) -> tuple[str, str]:
    """One LLM call: does this message satisfy the objective? Fail-closed."""
    try:
        from bob_server.services.llm_dispatch import LLMDispatchService

        response = await LLMDispatchService(ctx).chat(
            [{"role": "system", "content": _PROBE_SYSTEM},
             {"role": "user", "content":
              f"# Objective\n{goal['objective']}\n\n"
              f"# Message from {sender_name or 'the contact'} (in a different conversation)\n{text}"}],
            model=ctx.settings.goals.reviser_model
                or ctx.settings.openai.get_memory_model(),
            temperature=0.0,
            max_tokens=80,
            reasoning_effort="low",
            call_category="outreach_probe",
            session_key=goal["conversation_id"],
        )
        parsed = json.loads((response or "").strip())
        satisfied = bool(parsed.get("satisfied"))
        note = str(parsed.get("note", "") or "")
        return ("satisfied" if satisfied else "not_satisfied"), note
    except Exception:
        logger.warning("outreach probe call failed for goal %s", goal["id"],
                       exc_info=True)
        return "error", ""
