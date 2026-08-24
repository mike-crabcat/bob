"""Claim router — memory→goal routing (bob-events-plan.md Phase 2).

When a silent-turn extraction records claims, decide which active goals care
and fold the new information into their state — so a reply given in the
"wrong" channel still reaches the plan (gap G4, the core failure mode).

Design (plan §2.2–2.3):
- Trigger is INLINE at the extraction post-loop, backed by a durable
  ``memory.claims_created`` event_log row and a replay watermark — the
  in-process event_bus is telemetry only. A crash between extraction and
  routing is replayed by the heartbeat sweep, never lost.
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

Kill switch: ``BOB_CLAIM_ROUTER_DISABLED=1`` — extraction is unaffected and
the watermark does not advance, so re-enabling replays the gap.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import uuid4

from bob_server.context import AppContext

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
    from bob_server.repositories.history import HistoryRepository
    from bob_server.services.memory.claim_service import update_entity_mentions

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
    from bob_server.repositories.participants import ParticipantRepository
    return await ParticipantRepository(db).conversations_sharing_contacts(cid)


async def candidate_entity_ids(db: Any, session_key: str) -> list[tuple[str, str]]:
    """(entity_id, purpose) candidates for the extractor: refs of active goals
    held by this conversation, plus refs of goals held by conversations
    sharing human participants. Bounded to _MAX_CANDIDATE_ENTITIES."""
    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.repositories.goals import GoalRepository
    from bob_server.services.goal_state_service import parse_strategy

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
        for eid in parse_strategy(goal).refs.entities:
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
# Batch handling (plan §2.2 inline trigger + durable replay)
# ---------------------------------------------------------------------------

async def handle_extraction_batch(
    ctx: AppContext, *, session_key: str, turn_message_id: str,
) -> dict[str, Any]:
    """Durable record of the batch, then inline routing.

    The event_log row is appended BEFORE routing (accept-once on
    turn_message_id). If routing is disabled or crashes, the heartbeat sweep
    replays from the watermark once re-enabled/recovered."""
    from bob_server.repositories.event_log import Event, EventLogRepository

    db = ctx.db
    batch = await _batch_for_turn(db, turn_message_id)
    if not batch["claim_ids"]:
        return {"status": "no_claims"}

    from bob_server.repositories.conversations import ConversationRepository
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
            bus.publish(ROUTED_EVENT_TYPE, {
                "session_key": session_key, "event_id": event_id,
                "claims": len(batch["claim_ids"]),
            })
        except Exception:
            pass

    if router_disabled():
        return {"status": "disabled", "event_id": event_id}

    routed = await _route_batch(ctx, session_key=session_key, cid=cid,
                                turn_message_id=turn_message_id, batch=batch)
    return {"status": "routed", "event_id": event_id, **routed}


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


# ---------------------------------------------------------------------------
# Candidate matching + delivery (plan §2.3)
# ---------------------------------------------------------------------------

async def _candidate_goals(
    db: Any, cid: str, entity_ids: list[str],
) -> list[tuple[str, str]]:
    """(goal_id, match_type) candidates. Strong matches (ref, mention) skip
    the probe; participant overlap is weak and probed. The originating
    conversation is excluded from holder-based matching (echo suppression).
    Cross-domain reads go through the owning repositories."""
    from bob_server.repositories.goals import GoalRepository

    repo = GoalRepository(db)
    candidates: dict[str, str] = {}

    if entity_ids:
        for gid in await repo.active_goal_ids_referencing_entities(entity_ids):
            candidates[gid] = "ref"

        # Conversations that discussed these entities (memory-owned index),
        # then goals held by them.
        marks = ",".join("?" for _ in entity_ids)
        mention_rows = await db.fetch_all(
            f"SELECT DISTINCT conversation_id FROM memory_entity_mentions "
            f"WHERE entity_id IN ({marks})", tuple(entity_ids))
        mention_convs = [r["conversation_id"] for r in mention_rows or []]
        for gid in await repo.active_goal_ids_held_by_conversations(
                mention_convs, exclude_conversation_id=cid):
            candidates.setdefault(gid, "mention")

    overlap_convs = await _participant_overlapping_conversations(db, cid)
    for gid in await repo.active_goal_ids_held_by_conversations(
            overlap_convs, exclude_conversation_id=cid):
        candidates.setdefault(gid, "participant")

    return list(candidates.items())


_PROBE_SYSTEM = """\
You decide whether newly extracted memory facts are relevant to an active plan.
Given the plan's objective and current state, and the new facts, answer
RELEVANT if the facts could change the plan's decisions, state, or next
actions (attendance, availability, preferences, cancellations, venue facts,
sizes, approvals…). Answer IGNORE only if the facts are clearly unrelated to
the plan. When unsure, answer RELEVANT — a wrong IGNORE loses information
silently. Respond with ONLY a JSON object: {"verdict": "RELEVANT"|"IGNORE"}"""


async def _probe_relevance(ctx: AppContext, goal: dict[str, Any],
                           facts: str) -> str:
    """Cheap relevance gate for weak (participant-only) matches. Fails open
    to 'relevant' on any error (plan §2.3 asymmetry)."""
    try:
        from bob_server.services.llm_dispatch import LLMDispatchService

        result = await LLMDispatchService(ctx).chat(
            [{"role": "system", "content": _PROBE_SYSTEM},
             {"role": "user", "content":
              f"# Plan\n{goal['objective']}\n\n# New facts\n{facts}"}],
            model=ctx.settings.goals.reviser_model
                or ctx.settings.openai.get_memory_model(),
            temperature=0.0,
            max_tokens=60,
            reasoning_effort="low",
            call_category="claim_router_probe",
            session_key=goal["conversation_id"],
        )
        parsed = json.loads(result.strip())
        verdict = str(parsed.get("verdict", "")).upper()
        return "relevant" if verdict == "RELEVANT" else (
            "ignore" if verdict == "IGNORE" else "relevant")
    except Exception:
        logger.warning("claim router probe failed; failing open to relevant",
                       exc_info=True)
        return "error"  # caller treats error as relevant (fail open), logged distinctly


async def _render_stimulus(
    db: Any, session_key: str, cid: str, batch: dict[str, Any],
) -> str:
    from bob_server.repositories.conversations import ConversationRepository
    lines = [f"New memory extracted from conversation `{session_key}`:"]
    display = await _entity_display_names(db, batch["entity_ids"])
    for c in batch["claims"]:
        if c["object_id"]:
            val = f"→ {c['object_id']}" + (
                f" ({display[c['object_id']]})" if display.get(c["object_id"]) else "")
        else:
            val = f"= {c['value']}"
        subj = c["subject_id"] + (
            f" ({display[c['subject_id']]})" if display.get(c["subject_id"]) else "")
        lines.append(f"- {subj}: {c['claim_type_key']} {val}")

    conv = await ConversationRepository(db).get(cid)
    if conv and conv.get("kind") == "dm":
        lines.append(
            "Note: these facts were said in a private DM — mark them "
            "private_source and never repeat them verbatim into group messages.")
    return "\n".join(lines)


async def _entity_display_names(db: Any, entity_ids: list[str]) -> dict[str, str]:
    if not entity_ids:
        return {}
    marks = ",".join("?" for _ in entity_ids)
    rows = await db.fetch_all(
        f"SELECT entity_id, display_name FROM memory_entities "
        f"WHERE entity_id IN ({marks})", tuple(entity_ids))
    return {r["entity_id"]: r["display_name"] for r in rows or []}


async def _route_batch(
    ctx: AppContext, *, session_key: str, cid: str, turn_message_id: str,
    batch: dict[str, Any],
) -> dict[str, Any]:
    from bob_server.repositories.goals import GoalRepository
    from bob_server.services.goal_state_service import enqueue_revision

    stimulus = await _render_stimulus(ctx.db, session_key, cid, batch)
    goals = await _candidate_goals(ctx.db, cid, batch["entity_ids"])
    repo = GoalRepository(ctx.db)

    delivered = skipped = 0
    for goal_id, match_type in goals:
        goal = await repo.get(goal_id)
        if goal is None or goal["status"] != "active":
            continue

        probe_verdict = "skipped"
        if match_type == "participant":
            verdict = await _probe_relevance(ctx, goal, stimulus)
            # Fail open: probe errors deliver as if relevant.
            if verdict == "ignore":
                await _log_decision(ctx, turn_message_id, cid, goal_id, batch,
                                    match_type, probe_verdict="ignore",
                                    revise_outcome="skipped", wake="no_wake",
                                    detail="probe ignored")
                skipped += 1
                continue
            probe_verdict = verdict

        try:
            result = await enqueue_revision(
                ctx, goal_id, stimulus, stimulus_id=turn_message_id)
            ok = bool(result.get("ok"))
        except Exception:
            logger.exception("claim routing enqueue failed for goal %s", goal_id)
            ok = False
        await _log_decision(ctx, turn_message_id, cid, goal_id, batch,
                            match_type, probe_verdict=probe_verdict,
                            revise_outcome="enqueued" if ok else "error",
                            wake="pending",
                            detail="" if ok else "enqueue failed")
        delivered += 1 if ok else 0

    if goals:
        logger.info("claim router: %d claim(s) from %s → %d goal(s) "
                    "(%d probe-ignored)",
                    len(batch["claim_ids"]), session_key, delivered, skipped)
    return {"candidates": len(goals), "delivered": delivered,
            "probe_ignored": skipped}


async def _log_decision(
    ctx: AppContext, stimulus_id: str, source_cid: str, goal_id: str,
    batch: dict[str, Any], match_type: str, *, probe_verdict: str,
    revise_outcome: str, wake: str, detail: str = "",
) -> None:
    try:
        await ctx.db.execute(
            """INSERT INTO memory_routing_log
               (id, stimulus_id, source_conversation_id, goal_id, claim_ids,
                entity_ids, match_type, probe_verdict, revise_outcome,
                wake_decision, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), stimulus_id, source_cid, goal_id,
             json.dumps(batch["claim_ids"]), json.dumps(batch["entity_ids"]),
             match_type, probe_verdict, revise_outcome, wake,
             detail[:500] or None, _now_iso()),
        )
    except Exception:
        logger.warning("routing log write failed", exc_info=True)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Watermark + replay sweep (plan §2.2 durability)
# ---------------------------------------------------------------------------

async def get_watermark(db: Any) -> str | None:
    row = await db.fetch_one(
        "SELECT event_id FROM claim_router_watermark WHERE id = 1")
    return row["event_id"] if row else None


async def advance_watermark(db: Any, event_id: str) -> None:
    from bob_server.services.base import utcnow
    await db.execute(
        """INSERT INTO claim_router_watermark (id, event_id, updated_at)
           VALUES (1, ?, ?)
           ON CONFLICT (id) DO UPDATE SET event_id = excluded.event_id,
                                          updated_at = excluded.updated_at""",
        (event_id, utcnow().isoformat()),
    )


async def replay_pending(ctx: AppContext, *, limit: int = 20) -> int:
    """Heartbeat sweep: route any claims_created events past the watermark.
    Idempotent per (goal, stimulus) via the revise effect key, so an inline
    delivery followed by a replay is harmless."""
    if router_disabled():
        return 0
    wm = await get_watermark(ctx.db)
    if wm is None:
        # First run: position at the very start ("" sorts before every event
        # id) so nothing recorded while the router was off is skipped — the
        # plan's "re-enabling replays the gap" contract, applied to the
        # initial enable too. The 20-events-per-tick limit throttles the
        # backlog; pair with BOB_GOAL_STATE_SHADOW during burn-in.
        await advance_watermark(ctx.db, "")
        wm = ""

    from bob_server.repositories.event_log import EventLogRepository

    rows = await EventLogRepository(ctx.db).events_after(
        ROUTED_EVENT_TYPE, wm, limit=limit)
    for ev in rows or []:
        try:
            payload = json.loads(ev["payload_json"] or "{}")
            batch = await _batch_for_turn(ctx.db,
                                          payload.get("turn_message_id", ""))
            if batch["claim_ids"]:
                await _route_batch(
                    ctx, session_key=payload.get("session_key", ""),
                    cid=payload.get("conversation_id", ""),
                    turn_message_id=payload.get("turn_message_id", ""),
                    batch=batch)
        except Exception:
            logger.exception("claim router replay failed for event %s", ev["id"])
        # Advance per event: a failure in one batch never blocks the queue
        # (the revise effect's own retries cover redelivery).
        await advance_watermark(ctx.db, ev["id"])
    return len(rows or [])
