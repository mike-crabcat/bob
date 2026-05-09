"""Dashboard LLM call log viewer."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _get_settings, _render_template

router = APIRouter()


@router.get("/harness", response_class=HTMLResponse)
async def harness_logs(
    request: Request,
    db: Database = Depends(get_database),
    model: str | None = None,
    session_key: str | None = None,
    provider: str | None = None,
    call_category: str | None = None,
) -> HTMLResponse:
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )

    entries: list[dict] = []
    stats = {"total": 0, "completed": 0, "failed": 0, "avg_latency": 0.0}

    if table_exists:
        query = "SELECT * FROM llm_call_log"
        conditions = []
        params = []

        if model:
            conditions.append("model = ?")
            params.append(model)
        if session_key:
            conditions.append("session_key = ?")
            params.append(session_key)
        if provider:
            conditions.append("provider = ?")
            params.append(provider)
        if call_category:
            conditions.append("call_category = ?")
            params.append(call_category)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT 200"

        rows = await db.fetch_all(query, tuple(params))
        for row in rows:
            entries.append({
                "id": row["id"],
                "timestamp": row.get("created_at", "")[:19].replace("T", " "),
                "provider": row.get("provider", ""),
                "model": row["model"],
                "call_category": row.get("call_category", ""),
                "session_key": row.get("session_key", ""),
                "system_prompt": row.get("system_prompt", ""),
                "user_message": row.get("user_message", ""),
                "response": row.get("response_text", ""),
                "prompt_tokens": row.get("prompt_tokens"),
                "completion_tokens": row.get("completion_tokens"),
                "total_tokens": row.get("total_tokens"),
                "cached_tokens": row.get("cached_tokens"),
                "latency_seconds": row.get("latency_seconds"),
                "ttft_seconds": row.get("ttft_seconds"),
                "status": row["status"],
                "error_message": row.get("error_message"),
            })

        stats_row = await db.fetch_one(
            "SELECT COUNT(*) as total,"
            " SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,"
            " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,"
            " AVG(CASE WHEN latency_seconds IS NOT NULL THEN latency_seconds END) as avg_latency"
            " FROM llm_call_log WHERE created_at > datetime('now', '-24 hours')"
        )
        if stats_row:
            stats = {
                "total": stats_row["total"] or 0,
                "completed": stats_row["completed"] or 0,
                "failed": stats_row["failed"] or 0,
                "avg_latency": round(stats_row["avg_latency"], 2) if stats_row["avg_latency"] else 0.0,
            }

    return _render_template(
        "dashboard/harness.html",
        request,
        {
            "version": settings.version,
            "entries": entries,
            "stats": stats,
            "pending_count": pending_count,
            "filters": {
                "model": model or "",
                "session_key": session_key or "",
                "provider": provider or "",
                "call_category": call_category or "",
            },
        },
    )


@router.get("/harness/{log_id}", response_class=HTMLResponse)
async def harness_log_detail(
    request: Request,
    log_id: str,
    db: Database = Depends(get_database),
) -> HTMLResponse:
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    row = await db.fetch_one(
        "SELECT * FROM llm_call_log WHERE id = ?", (log_id,),
    )
    if not row:
        return HTMLResponse(content="Log entry not found", status_code=404)

    entry = {
        "id": row["id"],
        "timestamp": row.get("created_at", "")[:19].replace("T", " "),
        "provider": row.get("provider", ""),
        "model": row["model"],
        "call_category": row.get("call_category", ""),
        "session_key": row.get("session_key", ""),
        "system_prompt": row.get("system_prompt", ""),
        "user_message": row.get("user_message", ""),
        "response": row.get("response_text", ""),
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "total_tokens": row.get("total_tokens"),
        "cached_tokens": row.get("cached_tokens"),
        "latency_seconds": row.get("latency_seconds"),
        "ttft_seconds": row.get("ttft_seconds"),
        "status": row["status"],
        "error_message": row.get("error_message"),
        "messages_json": row.get("messages_json", "[]"),
        "messages": json.loads(row.get("messages_json", "[]")) if row.get("messages_json") else [],
    }

    return _render_template(
        "dashboard/harness_detail.html",
        request,
        {
            "version": settings.version,
            "entry": entry,
            "pending_count": pending_count,
        },
    )
