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

    # 4. Effects — primary match on bindings.address (truthful post-451);
    # key-tail parsing kept as last-resort fallback for pre-route rows
    # (e.g. agent:main:whatsapp:group:<id> -> <id>@g.us).
    segments = {k.rsplit(":", 1)[-1] for k in keys} | set(keys)
    for row in await db.fetch_all(
            "SELECT address FROM bindings WHERE conversation_id = ? AND address IS NOT NULL",
            (conversation_id,)):
        segments.add(row["address"])
        segments.add(str(row["address"]).split("@", 1)[0])
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


@router.get("/api/conversations/{session_key:path}/detail")
async def get_conversation_detail(request: Request, session_key: str) -> dict[str, Any]:
    """Session/conversation detail for the SPA (ported from the retired
    sessions.py router; endpoint lookup reads bindings, not session_routes)."""
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    session_context: dict[str, Any] = {
        "kind": None,
        "display_name": None,
        "description": None,
        "member_count": None,
        "email_participants": None,
    }
    binding = await db.fetch_one(
        "SELECT channel, endpoint_kind, address, contact_id FROM bindings "
        "WHERE session_key = ? AND is_active = 1",
        (session_key,),
    )
    if binding:
        kind = binding["endpoint_kind"]
        address = binding["address"]
        session_context["kind"] = kind

        if kind == "group" and address:
            group = await db.fetch_one(
                "SELECT name, description, member_count FROM whatsappgroups "
                "WHERE whatsapp_jid = ? AND deleted_at IS NULL",
                (address,),
            )
            if group:
                session_context["display_name"] = group["name"]
                session_context["description"] = group["description"]
                session_context["member_count"] = group["member_count"]

        elif kind == "thread" and address:
            thread = await db.fetch_one(
                "SELECT subject FROM email_threads "
                "WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
                (address,),
            )
            if thread:
                session_context["display_name"] = thread["subject"]
                email_parts = await db.fetch_all(
                    "SELECT DISTINCT sender_email, sender_name FROM email_messages em "
                    "INNER JOIN email_threads et ON et.id = em.thread_id "
                    "WHERE et.agentmail_thread_id = ? ORDER BY em.message_timestamp ASC",
                    (address,),
                )
                session_context["email_participants"] = [
                    {"email": p["sender_email"], "name": p["sender_name"]}
                    for p in email_parts
                ]

        elif kind == "dm" and binding["contact_id"]:
            contact = await db.fetch_one(
                "SELECT name FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (binding["contact_id"],),
            )
            if contact:
                session_context["display_name"] = contact["name"]

    calls: list[dict[str, Any]] = []
    rows = await db.fetch_all(
        """SELECT l.id, l.created_at, l.call_category, l.status, l.latency_seconds,
                  l.ttft_seconds, l.total_tokens, l.prompt_tokens, l.completion_tokens,
                  l.tool_blocks_json, l.user_message, l.response_text,
                  l.error_message, l.contact_id, l.model,
                  c.name as contact_name
           FROM llm_call_log l
           LEFT JOIN contacts c ON c.id = l.contact_id AND c.deleted_at IS NULL
           WHERE l.session_key = ?
           ORDER BY l.created_at DESC
           LIMIT 100""",
        (session_key,),
    )
    for row in rows:
        is_reflection = row.get("call_category") == "reflection"
        tool_count = 0
        trace_raw = row.get("tool_blocks_json")
        if trace_raw:
            try:
                tool_count = sum(
                    1 for it in json.loads(trace_raw)
                    if isinstance(it, dict) and it.get("type") == "function_call"
                )
            except (json.JSONDecodeError, TypeError):
                pass
        calls.append({
            "id": row["id"],
            "created_at": _utc(row["created_at"]),
            "call_category": row.get("call_category", ""),
            "status": row["status"],
            "latency_seconds": row.get("latency_seconds"),
            "ttft_seconds": row.get("ttft_seconds"),
            "total_tokens": row.get("total_tokens"),
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "tool_count": tool_count,
            "model": row.get("model", ""),
            "user_message": (row.get("user_message") or "") if is_reflection else (row.get("user_message") or "")[:300],
            "response_preview": (row.get("response_text") or "") if is_reflection else (row.get("response_text") or "")[:300],
            "error_message": row.get("error_message"),
            "contact_id": row.get("contact_id"),
            "contact_name": row.get("contact_name"),
        })

    participants: list[dict[str, Any]] = []
    p_rows = await db.fetch_all(
        "SELECT sp.display_name, sp.identifier, sp.contact_id, sp.is_trusted, sp.last_active_at, "
        "COALESCE(c.name, sp.display_name, sp.identifier) as resolved_name "
        "FROM session_participants sp "
        "LEFT JOIN contacts c ON c.id = sp.contact_id AND c.deleted_at IS NULL "
        "WHERE sp.session_key = ? ORDER BY sp.last_active_at DESC",
        (session_key,),
    )
    for row in p_rows:
        participants.append({
            "display_name": row["resolved_name"],
            "identifier": row["identifier"],
            "contact_id": row["contact_id"],
            "is_trusted": bool(row.get("is_trusted", 0)),
            "last_active": row["last_active_at"],
        })

    agenda_row = await db.fetch_one(
        "SELECT agenda FROM session_agendas WHERE session_key = ?", (session_key,)
    )
    current_agenda = agenda_row["agenda"] if agenda_row else ""

    messages: list[dict[str, Any]] = []
    m_rows = await db.fetch_all(
        "SELECT sm.id, sm.role, sm.content, sm.channel, sm.sender_id, sm.created_at, "
        "COALESCE(c.name, sp.display_name) as sender_name "
        "FROM session_messages sm "
        "LEFT JOIN contacts c ON c.id = sm.sender_id AND c.deleted_at IS NULL "
        "LEFT JOIN session_participants sp ON sp.contact_id = sm.sender_id AND sp.session_key = sm.session_key "
        "WHERE sm.rowid IN ("
        "  SELECT rowid FROM session_messages"
        "  WHERE session_key = ? ORDER BY created_at DESC LIMIT 200"
        ") ORDER BY sm.created_at ASC",
        (session_key,),
    )
    for row in m_rows:
        messages.append({
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "channel": row["channel"],
            "sender_id": row["sender_id"],
            "sender_name": row.get("sender_name"),
            "created_at": _utc(row["created_at"]),
        })

    return {
        "session_key": session_key,
        "channel": _parse_channel(session_key),
        "session_context": session_context,
        "calls": calls,
        "messages": messages,
        "participants": participants,
        "summaries": [],
        "current_agenda": current_agenda,
        "stats": {
            "total_calls": len(calls),
            "completed": sum(1 for c in calls if c["status"] == "completed"),
            "failed": sum(1 for c in calls if c["status"] == "failed"),
        },
    }


@router.put("/api/conversations/{session_key:path}/agenda")
async def put_conversation_agenda(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    agenda = (body.get("agenda") or "").strip()
    db = _db(request)
    now = _utc_now()
    await db.execute(
        """INSERT INTO session_agendas (session_key, agenda, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(session_key) DO UPDATE SET agenda = excluded.agenda, updated_at = excluded.updated_at""",
        (session_key, agenda, now),
    )
    return {"ok": True}


@router.post("/api/conversations/{session_key:path}/reflect")
async def post_conversation_reflect(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "query required"}

    from bob_server.context import AppContext
    from bob_server.services.reflection_service import ReflectionService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    service = ReflectionService(ctx)
    try:
        result = await service.reflect(session_key, query)
        return result
    except Exception as exc:
        logger.error("Reflection failed for session=%s: %s", session_key, exc)
        return {"error": "reflection failed", "detail": str(exc)}
