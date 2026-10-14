"""Claim router — memory→goal routing (bob-events-plan.md Phase 2).

When a silent-turn extraction records claims, decide which active goals care
and fold the new information into their state — so a reply given in the
"wrong" channel still reaches the plan (gap G4, the core failure mode).

Design (plan §2.2–2.3):
- Trigger is INLINE at the extraction post-loop. Each batch is also
  recorded as a ``memory.claims_created`` event_log row (the in-process
  event_bus is telemetry only). There is no replay: the heartbeat sweep and
  its ``_route_batch`` path were retired with the legacy goal-state
  pipeline (7936ea18), so a crash between extraction and routing loses
  that batch's routing.
- Routing is structural first, probed secondarily: candidate goals come from
  strategy ``refs.entities`` intersection (strong), the entity-mention index
  (strong), or human participant overlap on ``participants.contact_id``
  (weak — the agent has no contact row, so Bob's omnipresence can't
  over-match). Weak-only matches pass a cheap relevance probe that FAILS
  OPEN to RELEVANT: a wrong IGNORE loses information silently.
- Echo suppression: the originating conversation is excluded from
  holder-based candidate matching (a goal is still a candidate via direct
  refs or its other holders).
- Delivery is ``enqueue_revision`` — the reviser folds silently and decides
  whether a ``goal_progress`` wake is warranted (plan §1.3 contract, no
  autonomous actuation).

Kill switch: ``BOB_CLAIM_ROUTER_DISABLED=1`` — extraction is unaffected;
batches recorded while it is off are not routed later.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from server.context import AppContext

logger = logging.getLogger(__name__)

ROUTED_EVENT_TYPE = "memory.claims_created"
_MAX_CANDIDATE_ENTITIES = 10


def router_disabled() -> bool:
    return os.getenv("BOB_CLAIM_ROUTER_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Entity-mention maintenance (plan §2.1)
# ---------------------------------------------------------------------------

async def refresh_mentions_for_turn(db: Any, turn_message_id: str) -> None:
    """Post-extraction refresh: claims written during the turn reference the
    synthetic marker message, which only exists once the turn commits — so
    the write_claim-time index update skipped them. One pass over the turn's
    claims (subjects + entity-ref objects) upserts their intervals."""
    from server.repositories.history import HistoryRepository
    from server.services.memory.claim_service import update_entity_mentions

    marker = await HistoryRepository(db).messages_by_ids([turn_message_id])
    if not marker:
        return
    rows = await db.fetch_all(
        "SELECT subject_id, object_id FROM memory_claims "
        "WHERE source_messages LIKE ?",
        (f'%"{turn_message_id}"%',),
    )
    entity_ids = {r["subject_id"] for r in rows or []}
    entity_ids.update(r["object_id"] for r in rows or [] if r["object_id"])
    if entity_ids:
        await update_entity_mentions(db, sorted(entity_ids), [turn_message_id])


# ---------------------------------------------------------------------------
# Extraction seeding (plan §2.0 layer 1)
# ---------------------------------------------------------------------------

async def _participant_overlapping_conversations(db: Any, cid: str) -> list[str]:
    from server.repositories.participants import ParticipantRepository
    return await ParticipantRepository(db).conversations_sharing_contacts(cid)


async def candidate_entity_ids(db: Any, session_key: str) -> list[tuple[str, str]]:
    """(entity_id, purpose) candidates for the extractor: refs of active goals
    held by this conversation, plus refs of goals held by conversations
    sharing human participants. Bounded to _MAX_CANDIDATE_ENTITIES."""
    from server.repositories.conversations import ConversationRepository
    from server.repositories.goals import GoalRepository
    from server.services.goal_state_service import parse_strategy

    repo = GoalRepository(db)
    cid = await ConversationRepository(db).resolve_cid(session_key)

    goals = list(await repo.goals_held_by(cid, limit=5))
    related = await _participant_overlapping_conversations(db, cid)
    seen_cids = {cid}
    for other in related:
        if other in seen_cids:
            continue
        seen_cids.add(other)
        goals.extend(await repo.goals_held_by(other, limit=5))

    candidates: dict[str, str] = {}
    for goal in goals:
        state = parse_strategy(goal)
        entities = list(state.refs.entities)
        legacy = state.legacy_outreach or {}
        if isinstance(legacy, dict):
            legacy_refs = legacy.get("refs") or {}
            if isinstance(legacy_refs, dict):
                entities += [e for e in legacy_refs.get("entities") or []
                             if isinstance(e, str)]
        for eid in entities:
            if eid and eid not in candidates:
                candidates[eid] = (goal["objective"] or "")[:100]
            if len(candidates) >= _MAX_CANDIDATE_ENTITIES:
                break
        if len(candidates) >= _MAX_CANDIDATE_ENTITIES:
            break
    return list(candidates.items())


async def build_candidate_entities_block(db: Any, session_key: str) -> str:
    """Prompt block steering the extractor to reuse goal-relevant entity ids
    (plan §2.0 layer 1 — the primary cross-conversation identity mechanism)."""
    candidates = await candidate_entity_ids(db, session_key)
    if not candidates:
        return ""
    ids = [eid for eid, _ in candidates]
    marks = ",".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT entity_id, entity_type, display_name, status "
        f"FROM memory_entities WHERE entity_id IN ({marks})", tuple(ids))
    known = {r["entity_id"]: r for r in rows or []}

    lines = [
        "## Candidate entities for this conversation",
        "Active plans reference these entities. If the conversation refers to "
        "the same real-world thing, REUSE the existing entity id (get_entity / "
        "add_claim on it) — do not mint a near-duplicate id for it:",
    ]
    for eid, purpose in candidates:
        row = known.get(eid)
        if row is None:
            continue
        suffix = f" — referenced by plan: {purpose}" if purpose else ""
        lines.append(f"- `{eid}` ({row['entity_type']}, "
                     f"\"{row['display_name']}\"){suffix}")
    return "\n".join(lines) if len(lines) > 2 else ""


# ---------------------------------------------------------------------------
# Batch handling (plan §2.2 inline trigger)
# ---------------------------------------------------------------------------

async def handle_extraction_batch(
    ctx: AppContext, *, session_key: str, turn_message_id: str,
) -> dict[str, Any]:
    """Durable record of the batch, then inline routing.

    The event_log row is appended BEFORE routing (accept-once on
    turn_message_id). Nothing replays it: if routing is disabled or
    crashes, that batch is not routed."""
    from server.repositories.event_log import Event, EventLogRepository

    db = ctx.db
    batch = await _batch_for_turn(db, turn_message_id)
    if not batch["claim_ids"]:
        return {"status": "no_claims"}

    from server.repositories.conversations import ConversationRepository
    cid = await ConversationRepository(db).resolve_cid(session_key)

    event_id = await EventLogRepository(db).append(Event(
        event_type=ROUTED_EVENT_TYPE,
        binding_key=f"memory:{session_key}",
        conversation_id=cid,
        source="memory",
        external_id=turn_message_id,
        payload={
            "session_key": session_key,
            "conversation_id": cid,
            "turn_message_id": turn_message_id,
            "claim_ids": batch["claim_ids"],
            "entity_ids": batch["entity_ids"],
        },
    ))

    # Telemetry only — never a trigger (the bus is volatile).
    bus = getattr(ctx, "event_bus", None)
    if bus is not None and event_id:
        try:
            await bus.publish(ROUTED_EVENT_TYPE, {
                "session_key": session_key, "event_id": event_id,
                "claims": len(batch["claim_ids"]),
            })
        except Exception:
            pass

    if router_disabled():
        return {"status": "disabled", "event_id": event_id}

    # Goal rooms (docs/goal-rooms-plan.md D3–D6): broadcast the batch as
    # per-entity sensations BEFORE legacy routing — corrections (action)
    # wake subscribed rooms through the stimulus spine, adds (info) ride
    # their check-in digest. Room-origin batches are skipped whole (D6).
    # Independent kill switch: rooms off → no emission, legacy path unchanged.
    try:
        from server.services import goal_rooms
        await goal_rooms.emit_claim_sensations(
            ctx, session_key=session_key, turn_message_id=turn_message_id,
            batch=batch)
    except Exception:
        logger.exception("goal-rooms claim sensation emission failed for %s",
                         session_key)

    # Routing to goals is retired (task-registry Phase 4, 2026-09-19):
    # goal rooms receive claims via their sensation subscriptions; the
    # legacy candidate-match + probe + reviser-fold pipeline is deleted.
    # The event_log row + watermark remain the durable emission record.
    return {"status": "emitted", "event_id": event_id,
            "sensations": True}


async def _batch_for_turn(db: Any, turn_message_id: str) -> dict[str, Any]:
    rows = await db.fetch_all(
        "SELECT id, claim_type_key, subject_id, value, object_id "
        "FROM memory_claims WHERE status = 'active' AND source_messages LIKE ?",
        (f'%"{turn_message_id}"%',),
    )
    claims = [dict(r) for r in rows or []]
    entity_ids: list[str] = []
    for c in claims:
        for eid in (c["subject_id"], c["object_id"]):
            if eid and eid not in entity_ids:
                entity_ids.append(eid)
    return {"claims": claims, "claim_ids": [c["id"] for c in claims],
            "entity_ids": entity_ids}


async def superseded_subjects(db: Any, claim_ids: list[str]) -> set[str]:
    """Subjects of claims superseded by any of ``claim_ids`` (memory-owned
    read for the goal-rooms emitter: a supersession makes the entity's
    sensation action-level — corrections wake, adds digest)."""
    subjects: set[str] = set()
    for cid in claim_ids:
        row = await db.fetch_one(
            "SELECT subject_id FROM memory_claims "
            "WHERE status = 'superseded' AND superseded_by LIKE ? LIMIT 1",
            (f'%"{cid}"%',))
        if row and row["subject_id"]:
            subjects.add(row["subject_id"])
    return subjects


async def mentioned_entities_for_conversation(
    db: Any, conversation_id: str, limit: int = 12,
) -> list[str]:
    """Entities a conversation has discussed (the entity-mention index) —
    the goal-room seed set's generous half (goal-rooms plan D8)."""
    rows = await db.fetch_all(
        "SELECT DISTINCT entity_id FROM memory_entity_mentions "
        "WHERE conversation_id = ? ORDER BY entity_id LIMIT ?",
        (conversation_id, limit))
    return [r["entity_id"] for r in rows or []]


async def person_entity_for_name(db: Any, display_name: str) -> str | None:
    """The active person entity for a contact's display name (memory-owned
    read; the outreach auto-subscribe seam resolves contact → entity)."""
    if not display_name:
        return None
    row = await db.fetch_one(
        "SELECT entity_id FROM memory_entities "
        "WHERE entity_type = 'person' AND display_name = ? "
        "AND status = 'active' LIMIT 1",
        (display_name,))
    return row["entity_id"] if row else None


# ---------------------------------------------------------------------------
# Candidate matching + delivery (plan §2.3)
# ---------------------------------------------------------------------------

_PROBE_SYSTEM = """\
You decide whether newly extracted memory facts are relevant to an active plan.
Given the plan's objective and current state, and the new facts, answer
RELEVANT if the facts could change the plan's decisions, state, or next
actions (attendance, availability, preferences, cancellations, venue facts,
sizes, approvals…). Answer IGNORE only if the facts are clearly unrelated to
the plan. When unsure, answer RELEVANT — a wrong IGNORE loses information
silently. Respond with ONLY a JSON object: {"verdict": "RELEVANT"|"IGNORE"}"""


async def recent_routing_log(db: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest routing decisions — the dashboard's routing browser reads
    through here (memory tables stay owned by this package, plan §4.2)."""
    rows = await db.fetch_all(
        """SELECT id, stimulus_id, source_conversation_id, goal_id, claim_ids,
                  entity_ids, match_type, probe_verdict, revise_outcome,
                  wake_decision, detail, created_at
           FROM memory_routing_log ORDER BY created_at DESC LIMIT ?""",
        (limit,))
    return [dict(r) for r in rows] if rows else []
