"""Dashboard log and SSE routes."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _get_settings, _render_template

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def logs(
    request: Request,
    db: Database = Depends(get_database),
    level: str | None = None,
    event_type: str | None = None,
    project_id: str | None = None,
) -> HTMLResponse:
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='structured_logs'"
    )

    logs = []
    stats = {"error": 0, "warning": 0, "info": 0, "reasoning": 0}

    if table_exists:
        query = "SELECT * FROM structured_logs"
        conditions = []
        params = []

        if level:
            conditions.append("level = ?")
            params.append(level.upper())
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY timestamp DESC LIMIT 500"

        rows = await db.fetch_all(query, tuple(params))

        for row in rows:
            logs.append({
                "timestamp": row.get("timestamp", "")[:19].replace("T", " "),
                "level": row["level"],
                "logger": row.get("logger", ""),
                "message": row["message"],
                "event_type": row.get("event_type"),
                "project_id": row.get("project_id"),
                "duration_seconds": row.get("duration_seconds"),
                "extra_data": row.get("extra_data"),
            })

        stats_rows = await db.fetch_all(
            """
            SELECT
                level,
                event_type,
                COUNT(*) as count
            FROM structured_logs
            WHERE timestamp > datetime('now', '-24 hours')
            GROUP BY level, event_type
            """
        )

        for row in stats_rows:
            level_val = row["level"]
            if level_val == "ERROR":
                stats["error"] += row["count"]
            elif level_val == "WARNING":
                stats["warning"] += row["count"]
            elif level_val == "INFO":
                stats["info"] += row["count"]
            if row.get("event_type") == "reasoning_request":
                stats["reasoning"] += row["count"]

    if not logs:
        logs = [{
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "level": "INFO",
            "logger": "cyborg.dashboard",
            "message": "No logs yet. Logs will appear here once structured logging captures events.",
            "event_type": None,
            "project_id": None,
            "duration_seconds": None,
            "extra_data": None,
        }]

    return _render_template(
        "dashboard/logs.html",
        request,
        {
            "version": settings.version,
            "logs": logs,
            "stats": stats,
            "last_log_time": datetime.now(timezone.utc).isoformat(),
            "pending_count": pending_count,
        },
    )


@router.get("/events")
@router.get("/logs/stream")
async def dashboard_events(request: Request):
    from starlette.responses import StreamingResponse

    async def event_stream():
        while True:
            yield f"event: message\ndata: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.now(timezone.utc).isoformat()})}\n\n"
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
    )
