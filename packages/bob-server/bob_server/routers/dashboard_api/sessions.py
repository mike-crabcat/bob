"""Dashboard API: Sessions, agendas, reflection."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/sessions")
async def get_sessions(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    sessions: list[dict[str, Any]] = []
    log_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    msgs_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages'"
    )
    if log_exists:
        rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as call_count,
                      MAX(created_at) || 'Z' as last_activity,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                      ROUND(AVG(CASE WHEN latency_seconds IS NOT NULL THEN latency_seconds END), 2) as avg_latency
               FROM llm_call_log
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 100"""
        )
        for row in rows:
            key = row["session_key"]
            sessions.append({
                "session_key": key,
                "channel": _parse_channel(key),
                "call_count": row["call_count"],
                "completed": row["completed"],
                "failed": row["failed"],
                "avg_latency": row["avg_latency"] or 0.0,
                "last_activity": row["last_activity"],
            })
    # Include sessions that only have messages (no llm_call_log) — e.g. outreach targets
    if msgs_exists:
        seen = {s["session_key"] for s in sessions}
        msg_rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as msg_count,
                      MAX(created_at) || 'Z' as last_activity
               FROM session_messages
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 100"""
        )
        for row in msg_rows:
            key = row["session_key"]
            if key not in seen:
                sessions.append({
                    "session_key": key,
                    "channel": _parse_channel(key),
                    "call_count": 0,
                    "completed": 0,
                    "failed": 0,
                    "avg_latency": 0.0,
                    "last_activity": row["last_activity"],
                    "msg_count": row["msg_count"],
                })
        sessions.sort(key=lambda s: s.get("last_activity") or "", reverse=True)
    return {"sessions": sessions}


@router.get("/api/sessions/{session_key:path}")
async def get_session_detail(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    # Resolve session context (group name, thread subject, etc.)
    session_context: dict[str, Any] = {
        "kind": None,
        "display_name": None,
        "description": None,
        "member_count": None,
        "email_participants": None,
    }
    sr_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_routes'"
    )
    if sr_table:
        route = await db.fetch_one(
            "SELECT channel, kind, chat_id, contact_id FROM session_routes "
            "WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if route:
            kind = route["kind"]
            chat_id = route["chat_id"]
            session_context["kind"] = kind

            if kind == "group" and chat_id:
                wg_table = await db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='whatsappgroups'"
                )
                if wg_table:
                    group = await db.fetch_one(
                        "SELECT name, description, member_count FROM whatsappgroups "
                        "WHERE whatsapp_jid = ? AND deleted_at IS NULL",
                        (chat_id,),
                    )
                    if group:
                        session_context["display_name"] = group["name"]
                        session_context["description"] = group["description"]
                        session_context["member_count"] = group["member_count"]

            elif kind == "thread" and chat_id:
                et_table = await db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='email_threads'"
                )
                if et_table:
                    thread = await db.fetch_one(
                        "SELECT subject FROM email_threads "
                        "WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
                        (chat_id,),
                    )
                    if thread:
                        session_context["display_name"] = thread["subject"]
                        em_table = await db.fetch_one(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='email_messages'"
                        )
                        if em_table:
                            email_parts = await db.fetch_all(
                                "SELECT DISTINCT sender_email, sender_name FROM email_messages em "
                                "INNER JOIN email_threads et ON et.id = em.thread_id "
                                "WHERE et.agentmail_thread_id = ? ORDER BY em.message_timestamp ASC",
                                (chat_id,),
                            )
                            session_context["email_participants"] = [
                                {"email": p["sender_email"], "name": p["sender_name"]}
                                for p in email_parts
                            ]

            elif kind == "dm":
                contact_id = route["contact_id"]
                if contact_id:
                    contact = await db.fetch_one(
                        "SELECT name FROM contacts WHERE id = ? AND deleted_at IS NULL",
                        (contact_id,),
                    )
                    if contact:
                        session_context["display_name"] = contact["name"]

    calls: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT l.id, l.created_at, l.call_category, l.status, l.latency_seconds,
                      l.ttft_seconds, l.total_tokens, l.prompt_tokens, l.completion_tokens,
                      l.messages_json, l.user_message, l.response_text,
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
            msgs_raw = row.get("messages_json")
            if msgs_raw:
                try:
                    tool_count = json.loads(msgs_raw).count('"function_call"')
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
    participants_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_participants'"
    )
    if participants_table:
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

    summaries: list[dict[str, Any]] = []

    agenda_row = await db.fetch_one(
        "SELECT agenda FROM session_agendas WHERE session_key = ?", (session_key,)
    )
    current_agenda = agenda_row["agenda"] if agenda_row else ""

    # Session messages (conversation entries from session_messages table)
    messages: list[dict[str, Any]] = []
    msgs_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages'"
    )
    if msgs_table:
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
        "summaries": summaries,
        "current_agenda": current_agenda,
        "stats": {
            "total_calls": len(calls),
            "completed": sum(1 for c in calls if c["status"] == "completed"),
            "failed": sum(1 for c in calls if c["status"] == "failed"),
        },
    }


@router.put("/api/sessions/{session_key:path}/agenda")
async def put_agenda(request: Request, session_key: str) -> dict[str, Any]:
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


@router.post("/api/sessions/{session_key:path}/reflect")
async def post_reflect(request: Request, session_key: str) -> dict[str, Any]:
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


