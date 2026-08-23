"""Dashboard API: operational status — quota gate, effects outbox, goals,
wakeups, stuck turns (Dashboard v3 increment 1).

The health strip and "needs attention" card read GET /api/status; dead
effects can be retried (back to pending, pump redelivers) or discarded
(terminal 'discarded' status, kept for audit).
"""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/status")
async def get_status(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from bob_server.services import quota_gate

    effect_counts: dict[str, int] = {}
    for row in await db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM effects GROUP BY status"):
        effect_counts[row["status"]] = row["n"]

    dead_effects = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "attempt": r["attempt"],
            "error": r["error"],
            "payload_preview": (r["payload_json"] or "")[:200],
            "created_at": _utc(r["created_at"]),
        }
        for r in await db.fetch_all(
            """SELECT id, kind, attempt, error, payload_json, created_at
               FROM effects WHERE status = 'dead'
               ORDER BY created_at DESC LIMIT 20""")
    ]

    goals = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM goals WHERE status = 'active'")
    overdue_goals = [
        {
            "id": r["id"],
            "objective": (r["objective"] or "")[:160],
            "kind": r["kind"],
            "deadline": _utc(r["deadline"]),
            "conversation_id": r["conversation_id"],
        }
        for r in await db.fetch_all(
            """SELECT id, objective, kind, deadline, conversation_id FROM goals
               WHERE status = 'active' AND deadline IS NOT NULL
                 AND deadline < strftime('%Y-%m-%dT%H:%M:%S', 'now')
               ORDER BY deadline LIMIT 20""")
    ]

    wakeups = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM wakeups WHERE status = 'scheduled'")
    next_wakeup = await db.fetch_one(
        """SELECT not_before, kind FROM wakeups WHERE status = 'scheduled'
           ORDER BY not_before LIMIT 1""")

    # Stuck turns: still 'running' with an expired lease — a crash or hang.
    stuck_turns = [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "attempt": r["attempt"],
            "started_at": _utc(r["started_at"]),
            "lease_expires_at": _utc(r["lease_expires_at"]),
        }
        for r in await db.fetch_all(
            """SELECT id, conversation_id, attempt, started_at, lease_expires_at
               FROM turns
               WHERE status = 'running'
                 AND lease_expires_at IS NOT NULL
                 AND lease_expires_at < strftime('%Y-%m-%dT%H:%M:%f', 'now')
               ORDER BY started_at LIMIT 20""")
    ]

    # Undispatched inbound (last 48h; older rows are pre-Bob3 relics).
    undispatched = await db.fetch_one(
        """SELECT COUNT(*) AS n FROM session_messages
           WHERE role = 'user' AND dispatched = 0
             AND created_at >= datetime('now', '-48 hours')""")

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
            "active": (goals or {}).get("n", 0) or 0,
            "overdue": overdue_goals,
        },
        "wakeups": {
            "scheduled": (wakeups or {}).get("n", 0) or 0,
            "next": {
                "not_before": _utc(next_wakeup["not_before"]),
                "kind": next_wakeup["kind"],
            } if next_wakeup else None,
        },
        "stuck_turns": stuck_turns,
        "undispatched_48h": (undispatched or {}).get("n", 0) or 0,
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
    row = await db.fetch_one(
        """SELECT id, conversation_id, status,
                  (status IN ('pending','running')
                   AND lease_expires_at IS NOT NULL
                   AND lease_expires_at < strftime('%Y-%m-%dT%H:%M:%f', 'now')) AS stuck
           FROM turns WHERE id = ?""", (turn_id,))
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
    try:
        await bridge.wake_session(conversation_id)
    except Exception as exc:
        logger.warning("dashboard: stuck-turn retry failed for %s", turn_id, exc_info=True)
        return {"ok": False, "error": str(exc)}
    logger.info("dashboard: stuck turn %s re-armed by operator (%s)", turn_id, conversation_id)
    return {"ok": True}


@router.post("/api/effects/{effect_id}/retry")
async def retry_effect(effect_id: str, request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    changed = await db.execute(
        """UPDATE effects
           SET status = 'pending', attempt = 0,
               available_at = strftime('%Y-%m-%dT%H:%M:%f', 'now') || '+00:00'
           WHERE id = ? AND status = 'dead'""",
        (effect_id,))
    if not changed:
        return {"ok": False, "error": "effect not found or not dead"}
    logger.info("dashboard: dead effect %s requeued by operator", effect_id)
    return {"ok": True}


@router.post("/api/effects/{effect_id}/discard")
async def discard_effect(effect_id: str, request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    changed = await db.execute(
        """UPDATE effects
           SET status = 'discarded',
               error = COALESCE(error, '') || ' [discarded by operator]'
           WHERE id = ? AND status = 'dead'""",
        (effect_id,))
    if not changed:
        return {"ok": False, "error": "effect not found or not dead"}
    logger.info("dashboard: dead effect %s discarded by operator", effect_id)
    return {"ok": True}
