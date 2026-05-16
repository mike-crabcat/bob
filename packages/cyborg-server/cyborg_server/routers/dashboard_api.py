"""Dashboard HTTP API — serves page data as JSON for the React SPA."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request

from cyborg_server.database import Database

logger = logging.getLogger(__name__)

router = APIRouter()


def _utc(val: str | None) -> str | None:
    if val and not val.endswith("Z") and not val.endswith("+00:00"):
        return val + "Z"
    return val


def _parse_channel(session_key: str) -> str:
    if session_key.startswith("bobvoice:"):
        return "voice"
    if ":whatsapp:" in session_key:
        return "whatsapp"
    if ":email:" in session_key:
        return "email"
    return "other"


def _check_auth(request: Request) -> bool:
    settings = request.app.state.settings
    if not settings.dashboard_secret_configured:
        return True
    secret = request.query_params.get("secret", "")
    if not secret:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            secret = auth[7:]
    return secret == settings.dashboard_secret


def _db(request: Request) -> Database:
    return request.app.state.db


@router.get("/api/home")
async def get_home(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    # Active sessions
    active_sessions: list[dict[str, Any]] = []
    log_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    msgs_exists_home = await db.fetch_one(
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
               LIMIT 50"""
        )
        for row in rows:
            key = row["session_key"]
            active_sessions.append({
                "session_key": key,
                "channel": _parse_channel(key),
                "call_count": row["call_count"],
                "completed": row["completed"],
                "failed": row["failed"],
                "avg_latency": row["avg_latency"] or 0.0,
                "last_activity": row["last_activity"],
            })
    if msgs_exists_home:
        seen = {s["session_key"] for s in active_sessions}
        msg_rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as msg_count,
                      MAX(created_at) || 'Z' as last_activity
               FROM session_messages
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 50"""
        )
        for row in msg_rows:
            key = row["session_key"]
            if key not in seen:
                active_sessions.append({
                    "session_key": key,
                    "channel": _parse_channel(key),
                    "call_count": 0,
                    "completed": 0,
                    "failed": 0,
                    "avg_latency": 0.0,
                    "last_activity": row["last_activity"],
                    "msg_count": row["msg_count"],
                })
        active_sessions.sort(key=lambda s: s.get("last_activity") or "", reverse=True)

    # LLM calls chart: 24h by 15min buckets, stacked by call_category
    chart_buckets: list[dict[str, Any]] = []
    chart_categories: list[str] = []
    if log_exists:
        chart_rows = await db.fetch_all(
            """SELECT
                  strftime('%Y-%m-%dT%H:%M',
                      datetime(strftime('%s', created_at) - strftime('%s', created_at) % 900, 'unixepoch')
                  ) as interval_start,
                  call_category,
                  COUNT(*) as count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
               GROUP BY interval_start, call_category
               ORDER BY interval_start"""
        )
        bucket_map: dict[str, dict[str, int]] = {}
        categories: set[str] = set()
        for row in chart_rows:
            iv = row["interval_start"]
            cat = row["call_category"] or "other"
            categories.add(cat)
            bucket_map.setdefault(iv, {})[cat] = row["count"]
        if categories:
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            epoch = int(now.timestamp())
            start_epoch = ((epoch - 86400) // 900) * 900
            for i in range(96):
                ts = start_epoch + 900 * i
                key = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
                entry: dict[str, Any] = {"interval_start": key}
                for cat in sorted(categories):
                    entry[cat] = bucket_map.get(key, {}).get(cat, 0)
                chart_buckets.append(entry)
        chart_categories = sorted(categories)

    # Recent summaries
    recent_summaries: list[dict[str, Any]] = []
    summaries_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_summaries'"
    )
    if summaries_table:
        summary_rows = await db.fetch_all(
            """SELECT id, session_key, summary_text, topics, participants,
                      active_from, active_to, created_at
               FROM session_summaries
               ORDER BY created_at DESC
               LIMIT 3"""
        )
        for row in summary_rows:
            recent_summaries.append({
                "id": row["id"],
                "session_key": row["session_key"],
                "summary_text": row["summary_text"],
                "topics": json.loads(row["topics"]) if row["topics"] else [],
                "participants": json.loads(row["participants"]) if row["participants"] else [],
                "active_from": row["active_from"],
                "active_to": row["active_to"],
                "created_at": _utc(row["created_at"]),
            })

    # Active dispatches
    active_dispatches: list[dict[str, Any]] = []
    dispatch_rows = await db.fetch_all(
        """SELECT d.id, d.notification_type, d.session_key, d.task_id, d.project_id,
                  d.status, d.dispatched_at, d.tap_count,
                  t.title AS task_title, p.title AS project_title
           FROM dispatches d
           LEFT JOIN tasks t ON t.id = d.task_id AND t.deleted_at IS NULL
           LEFT JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
           WHERE d.status = 'active'
           ORDER BY d.dispatched_at ASC
           LIMIT 20"""
    )
    for row in dispatch_rows:
        active_dispatches.append({
            "id": row["id"],
            "notification_type": row["notification_type"],
            "session_key": row["session_key"],
            "task_id": row["task_id"],
            "task_title": row["task_title"],
            "project_title": row["project_title"],
            "dispatched_at": row["dispatched_at"],
            "tap_count": row["tap_count"],
        })

    # Project/task stats
    project_stats = {}
    ps = await db.fetch_one(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN state='planning' THEN 1 ELSE 0 END) as planning,
                  SUM(CASE WHEN state='active' THEN 1 ELSE 0 END) as active,
                  SUM(CASE WHEN state='paused' THEN 1 ELSE 0 END) as paused,
                  SUM(CASE WHEN state='closed' THEN 1 ELSE 0 END) as closed
           FROM projects WHERE deleted_at IS NULL"""
    )
    if ps:
        project_stats = {
            "planning": int(ps["planning"] or 0),
            "active": int(ps["active"] or 0),
            "paused": int(ps["paused"] or 0),
            "closed": int(ps["closed"] or 0),
        }

    task_rows = await db.fetch_all(
        "SELECT status, COUNT(*) as count FROM tasks WHERE deleted_at IS NULL GROUP BY status"
    )
    task_stats = {row["status"]: row["count"] for row in task_rows}

    # Recent activity
    recent_activities: list[dict[str, Any]] = []
    journal_rows = await db.fetch_all(
        """SELECT project_id, entry_type, content, created_at
           FROM project_journal_entries
           ORDER BY created_at DESC LIMIT 8"""
    )
    for row in journal_rows:
        content = row["content"] or ""
        recent_activities.append({
            "type": "journal",
            "label": row["entry_type"].replace("_", " ").title(),
            "summary": content[:140] + "..." if len(content) > 140 else content,
            "created_at": _utc(row["created_at"]),
            "project_id": row["project_id"],
        })

    notif_rows = await db.fetch_all(
        """SELECT title, message, notification_type, created_at
           FROM notifications ORDER BY created_at DESC LIMIT 8"""
    )
    for row in notif_rows:
        message = row["message"] or ""
        recent_activities.append({
            "type": "notification",
            "label": row["notification_type"].replace("_", " ").title(),
            "summary": message[:140] + "..." if len(message) > 140 else message,
            "created_at": _utc(row["created_at"]),
            "title": row["title"],
        })

    recent_activities.sort(key=lambda a: a.get("created_at") or "", reverse=True)

    return {
        "active_sessions": active_sessions,
        "chart_buckets": chart_buckets,
        "chart_categories": chart_categories,
        "recent_summaries": recent_summaries,
        "active_dispatches": active_dispatches,
        "project_stats": project_stats,
        "task_stats": task_stats,
        "recent_activities": recent_activities[:15],
    }


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

    calls: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT l.id, l.created_at, l.call_category, l.status, l.latency_seconds,
                      l.ttft_seconds, l.total_tokens, l.user_message, l.response_text,
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
            calls.append({
                "id": row["id"],
                "created_at": _utc(row["created_at"]),
                "call_category": row.get("call_category", ""),
                "status": row["status"],
                "latency_seconds": row.get("latency_seconds"),
                "ttft_seconds": row.get("ttft_seconds"),
                "total_tokens": row.get("total_tokens"),
                "model": row.get("model", ""),
                "user_message": (row.get("user_message") or "")[:300],
                "response_preview": (row.get("response_text") or "")[:300],
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
            "SELECT display_name, identifier, contact_id, is_trusted, last_active_at "
            "FROM session_participants WHERE session_key = ? ORDER BY last_active_at DESC",
            (session_key,),
        )
        for row in p_rows:
            participants.append({
                "display_name": row["display_name"] or row["identifier"],
                "identifier": row["identifier"],
                "contact_id": row["contact_id"],
                "is_trusted": bool(row.get("is_trusted", 0)),
                "last_active": row["last_active_at"],
            })

    summaries: list[dict[str, Any]] = []
    summaries_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_summaries'"
    )
    if summaries_table:
        s_rows = await db.fetch_all(
            """SELECT id, active_from, active_to, summary_text, topics,
                      participants, memory_prompts, message_count, created_at
               FROM session_summaries WHERE session_key = ?
               ORDER BY active_to DESC""",
            (session_key,),
        )
        for row in s_rows:
            summaries.append({
                "id": row["id"],
                "active_from": row["active_from"],
                "active_to": row["active_to"],
                "summary_text": row["summary_text"],
                "topics": json.loads(row["topics"]) if row["topics"] else [],
                "participants": json.loads(row["participants"]) if row["participants"] else [],
                "memory_prompts": json.loads(row["memory_prompts"]) if row["memory_prompts"] else [],
                "message_count": row["message_count"],
                "created_at": _utc(row["created_at"]),
            })

    agenda_row = await db.fetch_one(
        "SELECT agenda FROM session_agendas WHERE session_key = ?", (session_key,)
    )
    current_agenda = agenda_row["agenda"] if agenda_row else ""

    return {
        "session_key": session_key,
        "channel": _parse_channel(session_key),
        "calls": calls,
        "participants": participants,
        "summaries": summaries,
        "current_agenda": current_agenda,
        "stats": {
            "total_calls": len(calls),
            "completed": sum(1 for c in calls if c["status"] == "completed"),
            "failed": sum(1 for c in calls if c["status"] == "failed"),
        },
    }


@router.get("/api/contacts")
async def get_contacts(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    contacts: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT c.id, c.name, c.phone_number, c.email,
                      c.is_trusted, c.is_default,
                      c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM session_participants sp WHERE sp.contact_id = c.id) as session_count,
                      (SELECT MAX(sp.last_active_at) FROM session_participants sp WHERE sp.contact_id = c.id) as last_active
               FROM contacts c
               WHERE c.deleted_at IS NULL
               ORDER BY c.name"""
        )
        for row in rows:
            contacts.append({
                "id": row["id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "email": row["email"],
                "is_trusted": bool(row["is_trusted"]),
                "is_default": bool(row["is_default"]),
                "session_count": row["session_count"],
                "last_active": _utc(row["last_active"]),
                "created_at": _utc(row["created_at"]),
                "updated_at": _utc(row["updated_at"]),
            })
    return {"contacts": contacts}


@router.get("/api/contacts/{contact_id}")
async def get_contact_detail(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    contact = await db.fetch_one(
        """SELECT id, name, phone_number, email, whatsapp_groups, metadata,
                  is_trusted, is_default, created_at, updated_at
           FROM contacts WHERE id = ? AND deleted_at IS NULL""",
        (contact_id,),
    )
    if not contact:
        return {"id": None}

    sessions: list[dict[str, Any]] = []
    participants_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_participants'"
    )
    if participants_table:
        session_rows = await db.fetch_all(
            """SELECT sp.session_key, sp.last_active_at,
                      (SELECT COUNT(*) FROM llm_call_log l WHERE l.session_key = sp.session_key) as call_count
               FROM session_participants sp
               WHERE sp.contact_id = ?
               ORDER BY sp.last_active_at DESC""",
            (contact_id,),
        )
        for row in session_rows:
            sessions.append({
                "session_key": row["session_key"],
                "channel": _parse_channel(row["session_key"]),
                "call_count": row["call_count"],
                "last_active": _utc(row["last_active_at"]),
            })

    return {
        "id": contact["id"],
        "name": contact["name"],
        "phone_number": contact["phone_number"],
        "email": contact["email"],
        "is_trusted": bool(contact["is_trusted"]),
        "is_default": bool(contact["is_default"]),
        "whatsapp_groups": json.loads(contact["whatsapp_groups"]) if contact["whatsapp_groups"] else [],
        "metadata": json.loads(contact["metadata"]) if contact["metadata"] else {},
        "sessions": sessions,
        "created_at": _utc(contact["created_at"]),
        "updated_at": _utc(contact["updated_at"]),
    }


@router.get("/api/calls/{call_id}")
async def get_call_detail(request: Request, call_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    row = await db.fetch_one(
        """SELECT id, created_at, provider, model, call_category, session_key,
                  system_prompt, user_message, messages_json, tools_json,
                  response_text, latency_seconds, ttft_seconds,
                  prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                  status, error_message
           FROM llm_call_log WHERE id = ?""",
        (call_id,),
    )
    if not row:
        return {"error": "not found"}

    messages: list[dict[str, Any]] | None = None
    if row["messages_json"]:
        try:
            messages = json.loads(row["messages_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    tools: list[dict[str, Any]] | None = None
    if row["tools_json"]:
        try:
            tools = json.loads(row["tools_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "id": row["id"],
        "created_at": _utc(row["created_at"]),
        "provider": row["provider"],
        "model": row["model"],
        "call_category": row["call_category"],
        "session_key": row["session_key"],
        "status": row["status"],
        "latency_seconds": row["latency_seconds"],
        "ttft_seconds": row["ttft_seconds"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "cached_tokens": row["cached_tokens"],
        "messages": messages,
        "tools": tools,
        "response_text": row["response_text"],
        "user_message": row["user_message"],
        "system_prompt": row["system_prompt"],
        "error_message": row["error_message"],
    }
