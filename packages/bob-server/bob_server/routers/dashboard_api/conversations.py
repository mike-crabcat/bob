"""Dashboard API: Conversations (Bob3) — list with binding chips, decision
timeline, bindings/merge provenance (Dashboard v3 increment 2).

A conversation's timeline merges five sources: attention_shadow decisions,
tier-2 probe LLM calls, turns, effects, and goal transitions. Timestamps are
normalized (attention_shadow and llm_call_log use space-format local SQLite
timestamps; turns/effects/goals use ISO-T).
"""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


def _norm_ts(value: str | None) -> str:
    if not value:
        return ""
    ts = value.replace(" ", "T")
    if "+" not in ts and not ts.endswith("Z"):
        ts += "Z"
    return ts


async def _conversation_keys(db: Database, conversation_id: str) -> list[str]:
    """All session_keys bound to this conversation (incl. its own id, since
    conversation ids are legacy session_keys 1:1 today)."""
    keys = {conversation_id}
    for row in await db.fetch_all(
            "SELECT session_key FROM bindings WHERE conversation_id = ?",
            (conversation_id,)):
        keys.add(row["session_key"])
    return list(keys)


@router.get("/api/conversations")
async def list_conversations(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    rows = await db.fetch_all(
        """SELECT c.id, c.kind, c.title, c.merged_into, c.updated_at,
                  (SELECT COUNT(*) FROM bindings b WHERE b.conversation_id = c.id) AS binding_count,
                  (SELECT MAX(t.created_at) FROM turns t WHERE t.conversation_id = c.id) AS last_turn_at,
                  (SELECT replace(MAX(l.created_at), ' ', 'T') FROM llm_call_log l
                   WHERE l.session_key = c.id) AS last_llm_at,
                  (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id) AS turn_count,
                  (SELECT COUNT(*) FROM goals g WHERE g.conversation_id = c.id AND g.status = 'active') AS active_goals
           FROM conversations c
           ORDER BY COALESCE(NULLIF(MAX(COALESCE(last_turn_at, ''), COALESCE(last_llm_at, '')), ''), c.updated_at) DESC
           LIMIT 200""")

    conv_ids = [r["id"] for r in rows]
    bindings_by_conv: dict[str, list[dict[str, Any]]] = {}
    if conv_ids:
        marks = ",".join("?" * len(conv_ids))
        for b in await db.fetch_all(
                f"""SELECT conversation_id, session_key, channel, kind, address,
                           merged_from, merged_at
                    FROM bindings WHERE conversation_id IN ({marks})""",
                tuple(conv_ids)):
            bindings_by_conv.setdefault(b["conversation_id"], []).append({
                "session_key": b["session_key"],
                "channel": b["channel"],
                "kind": b["kind"],
                "address": b["address"],
                "merged": bool(b["merged_from"]),
            })

    conversations = []
    for r in rows:
        conversations.append({
            "id": r["id"],
            "kind": r["kind"],
            "title": r["title"],
            "merged_into": r["merged_into"],
            "channel": _parse_channel(r["id"]),
            "binding_count": r["binding_count"],
            "bindings": bindings_by_conv.get(r["id"], []),
            "turn_count": r["turn_count"],
            "active_goals": r["active_goals"],
            "last_activity": _norm_ts(
                max(r["last_turn_at"] or "", r["last_llm_at"] or "") or r["updated_at"]),
        })
    return {"conversations": conversations}


@router.get("/api/conversations/{conversation_id:path}/timeline")
async def get_timeline(request: Request, conversation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    keys = await _conversation_keys(db, conversation_id)
    marks = ",".join("?" * len(keys))
    items: list[dict[str, Any]] = []

    # 1. Attention decisions (tier-1 shadow rows; space-format timestamps)
    for r in await db.fetch_all(
            f"""SELECT decision, addressed, addressed_reason, source,
                       proposed_window_ms, created_at
                FROM attention_shadow WHERE session_key IN ({marks})
                ORDER BY created_at DESC LIMIT 100""", tuple(keys)):
        items.append({
            "type": "attention",
            "at": _norm_ts(r["created_at"]),
            "decision": r["decision"],
            "addressed": bool(r["addressed"]),
            "reason": r["addressed_reason"],
            "source": r["source"],
            "window_ms": r["proposed_window_ms"],
        })

    # 2. Tier-2 probe reasoning (llm_call_log; space-format timestamps)
    for r in await db.fetch_all(
            f"""SELECT created_at, response_text, status, latency_seconds
                FROM llm_call_log
                WHERE call_category = 'attention_probe' AND session_key IN ({marks})
                ORDER BY created_at DESC LIMIT 50""", tuple(keys)):
        decision, reason = None, None
        try:
            parsed = json.loads(r["response_text"] or "")
            decision, reason = parsed.get("decision"), parsed.get("reason")
        except (json.JSONDecodeError, TypeError):
            reason = (r["response_text"] or "")[:200]
        items.append({
            "type": "probe",
            "at": _norm_ts(r["created_at"]),
            "decision": decision,
            "reason": reason,
            "status": r["status"],
            "latency_seconds": r["latency_seconds"],
        })

    # 3. Turns
    for r in await db.fetch_all(
            """SELECT id, status, attempt, created_at, started_at, completed_at, error
               FROM turns WHERE conversation_id = ?
               ORDER BY created_at DESC LIMIT 100""", (conversation_id,)):
        items.append({
            "type": "turn",
            "at": _norm_ts(r["created_at"]),
            "id": r["id"],
            "status": r["status"],
            "attempt": r["attempt"],
            "completed_at": _norm_ts(r["completed_at"]),
            "error": r["error"],
        })

    # 4. Effects — bindings.address is often NULL; the routable address lives
    # in the session_key tail (e.g. agent:main:whatsapp:group:<id> ->
    # <id>@g.us). Match payload chat_id/to/session_key against key segments.
    segments = {k.rsplit(":", 1)[-1] for k in keys} | set(keys)
    effect_rows = await db.fetch_all(
        f"""SELECT id, kind, status, attempt, error, created_at,
                   COALESCE(json_extract(payload_json, '$.origin_session_key'),
                            json_extract(payload_json, '$.chat_id'),
                            json_extract(payload_json, '$.to'),
                            json_extract(payload_json, '$.session_key'), '') AS target,
                   substr(payload_json, 1, 200) AS payload_preview
            FROM effects
            WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id = ?)
               OR turn_id = '' OR turn_id IS NULL
            ORDER BY created_at DESC LIMIT 500""",
        (conversation_id,))
    matched = 0
    for r in effect_rows:
        target = str(r["target"] or "")
        bare = target.split("@", 1)[0]
        if not target or not (target in segments or bare in segments):
            continue
        matched += 1
        if matched > 100:
            break
        items.append({
            "type": "effect",
            "at": _norm_ts(r["created_at"]),
            "id": r["id"],
            "kind": r["kind"],
            "status": r["status"],
            "attempt": r["attempt"],
            "error": r["error"],
            "payload_preview": r["payload_preview"],
        })

    # 5. Goal transitions
    for r in await db.fetch_all(
            """SELECT gt.created_at, gt.from_status, gt.to_status, gt.note,
                      g.id AS goal_id, g.objective
               FROM goal_transitions gt JOIN goals g ON g.id = gt.goal_id
               WHERE g.conversation_id = ?
               ORDER BY gt.created_at DESC LIMIT 50""", (conversation_id,)):
        items.append({
            "type": "goal",
            "at": _norm_ts(r["created_at"]),
            "goal_id": r["goal_id"],
            "objective": (r["objective"] or "")[:160],
            "from_status": r["from_status"],
            "to_status": r["to_status"],
            "note": r["note"],
        })

    items.sort(key=lambda x: x["at"], reverse=True)
    return {"conversation_id": conversation_id, "items": items[:200]}


@router.get("/api/conversations/{conversation_id:path}/bindings")
async def get_bindings(request: Request, conversation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    conv = await db.fetch_one(
        "SELECT id, kind, title, merged_into FROM conversations WHERE id = ?",
        (conversation_id,))
    bindings = [
        {
            "session_key": r["session_key"],
            "channel": r["channel"],
            "kind": r["kind"],
            "address": r["address"],
            "sensitivity": r["sensitivity"],
            "merged_from": r["merged_from"],
            "merged_at": _norm_ts(r["merged_at"]),
            "created_at": _norm_ts(r["created_at"]),
        }
        for r in await db.fetch_all(
            """SELECT session_key, channel, kind, address, sensitivity,
                      merged_from, merged_at, created_at
               FROM bindings WHERE conversation_id = ?
               ORDER BY created_at""", (conversation_id,))
    ]
    return {"conversation": dict(conv) if conv else None, "bindings": bindings}


@router.post("/api/bindings/{session_key:path}/unmerge")
async def post_unmerge(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.repositories.conversations import ConversationRepository

    repo = ConversationRepository(_db(request))
    restored = await repo.unmerge(session_key)
    if restored is None:
        return {"ok": False, "error": "binding not merged or not found"}
    logger.info("dashboard: unmerged binding %s -> %s", session_key, restored)
    return {"ok": True, "conversation_id": restored}
