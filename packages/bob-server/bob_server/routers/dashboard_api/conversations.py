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
from bob_server.repositories.conversations import ConversationRepository


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
    for b in await ConversationRepository(db).bindings_for(conversation_id):
        keys.add(b["session_key"])
    return list(keys)


@router.get("/api/conversations")
async def list_conversations(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    rows = await ConversationRepository(db).dashboard_overview(limit=200)

    conv_ids = [r["id"] for r in rows]
    bindings_by_conv: dict[str, list[dict[str, Any]]] = {}
    for b in await ConversationRepository(db).bindings_for_many(conv_ids):
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
    items: list[dict[str, Any]] = []

    # 1. Attention decisions (tier-1 shadow rows; space-format timestamps)
    from bob_server.services.attention import shadow as attention_shadow
    for r in await attention_shadow.recent_decisions(db, list(keys), limit=100):
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
    from bob_server.repositories.llm_call_log import LlmCallLogRepository
    for r in await LlmCallLogRepository(db).probe_decisions(list(keys), limit=50):
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
    from bob_server.repositories.turns import TurnRepository
    for r in await TurnRepository(db).recent_for_conversation(conversation_id, limit=100):
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
    for b in await ConversationRepository(db).bindings_for(conversation_id):
        if b["address"]:
            segments.add(b["address"])
            segments.add(str(b["address"]).split("@", 1)[0])
    from bob_server.repositories.effects import EffectRepository
    effect_rows = await EffectRepository(db).timeline_candidates(conversation_id, limit=500)
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
    from bob_server.repositories.goals import GoalRepository
    for r in await GoalRepository(db).recent_transitions(conversation_id, limit=50):
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
    conv = await ConversationRepository(db).get(conversation_id)
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
        for r in await ConversationRepository(db).bindings_for(conversation_id)
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
    binding = await ConversationRepository(db).active_binding(session_key)
    if binding:
        kind = binding["endpoint_kind"]
        address = binding["address"]
        session_context["kind"] = kind

        if kind == "group" and address:
            from bob_server.repositories.groups import GroupRepository
            group = await GroupRepository(db).get_by_jid(address)
            if group:
                session_context["display_name"] = group["name"]
                session_context["description"] = group["description"]
                session_context["member_count"] = group["member_count"]

        elif kind == "thread" and address:
            from bob_server.services.email_store import EmailStore
            _estore = EmailStore(db)
            thread = await _estore.thread_by_agentmail_any(address)
            if thread:
                session_context["display_name"] = thread["subject"]
                email_parts = await _estore.thread_participants(address)
                session_context["email_participants"] = [
                    {"email": p["sender_email"], "name": p["sender_name"]}
                    for p in email_parts
                ]

        elif kind == "dm" and binding["contact_id"]:
            from bob_server.repositories.contacts import ContactRepository
            contact = await ContactRepository(db).get(binding["contact_id"])
            if contact:
                session_context["display_name"] = contact["name"]

    calls: list[dict[str, Any]] = []
    from bob_server.repositories.llm_call_log import LlmCallLogRepository
    rows = await LlmCallLogRepository(db).session_calls_with_contact(
        session_key, limit=100)
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
    cid = await ConversationRepository(db).resolve_cid(session_key)
    from bob_server.repositories.participants import ParticipantRepository
    p_rows = await ParticipantRepository(db).with_contact_names(cid)
    for row in p_rows:
        participants.append({
            "display_name": row["resolved_name"],
            "identifier": row["identifier"],
            "contact_id": row["contact_id"],
            "is_trusted": bool(row.get("is_trusted", 0)),
            "last_active": row["last_active_at"],
        })

    from bob_server.repositories.participants import AgendaRepository
    current_agenda = (await AgendaRepository(db).get(cid)) or ""

    messages: list[dict[str, Any]] = []
    from bob_server.repositories.history import HistoryRepository
    m_rows = await HistoryRepository(db).detail_messages(session_key, limit=200)
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
    from bob_server.repositories.participants import AgendaRepository
    await AgendaRepository(db).set(session_key, agenda, now)
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
