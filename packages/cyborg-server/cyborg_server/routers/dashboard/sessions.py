"""Dashboard session viewer — shows sessions from llm_call_log with drill-down."""

from __future__ import annotations

import json
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _get_settings, _render_template

router = APIRouter()


def _parse_channel(session_key: str) -> str:
    if session_key.startswith("bobvoice:"):
        return "voice"
    if ":whatsapp:" in session_key:
        return "whatsapp"
    if ":email:" in session_key:
        return "email"
    return "other"


@router.get("/sessions", response_class=HTMLResponse)
async def session_list(
    request: Request,
    db: Database = Depends(get_database),
) -> HTMLResponse:
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    entries: list[dict] = []

    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as call_count,
                      MAX(created_at) as last_activity,
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
            entries.append({
                "session_key": key,
                "session_key_enc": quote(key, safe=""),
                "channel": _parse_channel(key),
                "call_count": row["call_count"],
                "completed": row["completed"],
                "failed": row["failed"],
                "avg_latency": row["avg_latency"] or 0.0,
                "last_activity": (row["last_activity"] or "")[:19].replace("T", " "),
            })

    return _render_template(
        "dashboard/sessions.html",
        request,
        {
            "version": settings.version,
            "entries": entries,
            "pending_count": pending_count,
        },
    )


@router.get("/sessions/{session_key:path}", response_class=HTMLResponse)
async def session_detail(
    request: Request,
    session_key: str,
    db: Database = Depends(get_database),
) -> HTMLResponse:
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    session_key = unquote(session_key)
    channel = _parse_channel(session_key)

    # LLM calls for this session
    calls: list[dict] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT id, created_at, call_category, status, latency_seconds,
                      ttft_seconds, total_tokens, user_message, response_text, error_message
               FROM llm_call_log
               WHERE session_key = ?
               ORDER BY created_at DESC""",
            (session_key,),
        )
        for row in rows:
            calls.append({
                "id": row["id"],
                "timestamp": (row["created_at"] or "")[:19].replace("T", " "),
                "call_category": row.get("call_category", ""),
                "status": row["status"],
                "latency_seconds": row.get("latency_seconds"),
                "ttft_seconds": row.get("ttft_seconds"),
                "total_tokens": row.get("total_tokens"),
                "user_message": row.get("user_message", ""),
                "response_preview": (row.get("response_text") or "")[:200],
                "error_message": row.get("error_message"),
            })

    # Stats
    total_calls = len(calls)
    completed = sum(1 for c in calls if c["status"] == "completed")
    failed = sum(1 for c in calls if c["status"] == "failed")

    return _render_template(
        "dashboard/session_detail.html",
        request,
        {
            "version": settings.version,
            "session_key": session_key,
            "channel": channel,
            "calls": calls,
            "stats": {
                "total_calls": total_calls,
                "completed": completed,
                "failed": failed,
            },
            "pending_count": pending_count,
        },
    )
