"""Dashboard API: operational status — quota gate, effects outbox, goals,
wakeups, stuck turns (Dashboard v3 increment 1).

The health strip and "needs attention" card read GET /api/status; dead
effects can be retried (back to pending, pump redelivers) or discarded
(terminal 'discarded' status, kept for audit).
"""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/status")
async def get_status(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from server.services import quota_gate

    from server.repositories.effects import EffectRepository
    from server.repositories.turns import TurnRepository
    from server.repositories.goals import GoalRepository
    from server.repositories.wakeups import WakeupRepository
    effect_counts = await EffectRepository(db).status_counts()

    dead_effects = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "attempt": r["attempt"],
            "error": r["error"],
            "payload_preview": (r["payload_json"] or "")[:200],
            "created_at": _utc(r["created_at"]),
        }
        for r in await EffectRepository(db).dead(limit=20)
    ]

    goal_repo = GoalRepository(db)
    active_goal_count = await goal_repo.active_count()
    overdue_goals = [
        {
            "id": r["id"],
            "objective": (r["objective"] or "")[:160],
            "kind": r["kind"],
            "deadline": _utc(r["deadline"]),
            "conversation_id": r["conversation_id"],
        }
        for r in await goal_repo.overdue(limit=20)
    ]

    wakeup_repo = WakeupRepository(db)
    scheduled_wakeup_count = await wakeup_repo.scheduled_count()
    next_wakeup = await wakeup_repo.next_scheduled()

    # Stuck turns: still 'running' with an expired lease — a crash or hang.
    stuck_turns = [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "attempt": r["attempt"],
            "started_at": _utc(r["started_at"]),
            "lease_expires_at": _utc(r["lease_expires_at"]),
        }
        for r in await TurnRepository(db).stuck(limit=20)
    ]

    # Undispatched inbound (last 48h; older rows are pre-Bob3 relics).
    from server.repositories.history import HistoryRepository
    undispatched_n = await HistoryRepository(db).undispatched_count(hours=48)

    size_row = await db.fetch_one(
        "SELECT page_count * page_size AS bytes FROM pragma_page_count(), pragma_page_size()")

    return {
        "quota_gate": quota_gate.status(),
        "effects": {
            "counts": effect_counts,
            "pending": effect_counts.get("pending", 0) + effect_counts.get("delivering", 0),
            "dead": effect_counts.get("dead", 0),
            "dead_effects": dead_effects,
        },
        "goals": {
            "active": active_goal_count,
            "overdue": overdue_goals,
        },
        "wakeups": {
            "scheduled": scheduled_wakeup_count,
            "next": {
                "not_before": _utc(next_wakeup["not_before"]),
                "kind": next_wakeup["kind"],
            } if next_wakeup else None,
        },
        "stuck_turns": stuck_turns,
        "undispatched_48h": undispatched_n,
        "db_bytes": (size_row or {}).get("bytes", 0) or 0,
    }


@router.post("/api/turns/{turn_id}/retry")
async def retry_turn(turn_id: str, request: Request) -> dict[str, Any]:
    """Re-arm dispatch for a stuck turn's conversation. The turn claim path
    itself releases the expired lease and re-claims its events, so this just
    pushes a new dispatch through the normal inbound pipeline."""
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.turns import TurnRepository
    row = await TurnRepository(db).stuck_check(turn_id)
    if not row:
        return {"ok": False, "error": "turn not found"}
    if not row["stuck"]:
        return {"ok": False, "error": "turn is not stuck (still leased or already settled)"}

    conversation_id = row["conversation_id"]
    if ":whatsapp:" not in (conversation_id or ""):
        return {"ok": False, "error": f"retry not supported for channel of {conversation_id}"}

    bridge = getattr(request.app.state, "whatsapp_bridge_service", None)
    if bridge is None:
        return {"ok": False, "error": "whatsapp bridge not running"}

    # The killed dispatch already marked its user messages dispatched=1, and
    # DispatchRunner refuses to run with nothing pending — restore the zombie
    # turn's claimed messages first so the retry has something to claim.
    from server.repositories.history import HistoryRepository
    restored = await HistoryRepository(db).restore_messages_for_turn(turn_id)
    try:
        await bridge.wake_session(conversation_id)
    except Exception as exc:
        logger.warning("dashboard: stuck-turn retry failed for %s", turn_id, exc_info=True)
        return {"ok": False, "error": str(exc)}
    logger.info("dashboard: stuck turn %s re-armed by operator (%s, %s message(s) restored)",
                turn_id, conversation_id, restored)
    return {"ok": True, "restored_messages": restored}


@router.post("/api/effects/{effect_id}/retry")
async def retry_effect(effect_id: str, request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.effects import EffectRepository
    changed = await EffectRepository(db).requeue_dead(effect_id)
    if not changed:
        return {"ok": False, "error": "effect not found or not dead"}
    logger.info("dashboard: dead effect %s requeued by operator", effect_id)
    return {"ok": True}


@router.post("/api/effects/{effect_id}/discard")
async def discard_effect(effect_id: str, request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.effects import EffectRepository
    changed = await EffectRepository(db).discard_dead(effect_id)
    if not changed:
        return {"ok": False, "error": "effect not found or not dead"}
    logger.info("dashboard: dead effect %s discarded by operator", effect_id)
    return {"ok": True}
