"""OpenClaw plugin integration for context injection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from cyborg.dependencies import get_database
from cyborg.database import Database


router = APIRouter(prefix="/openclaw", tags=["openclaw"])


@router.get("/context.txt", response_class=PlainTextResponse)
async def get_openclaw_context(
    request: Request,
    db: Database = Depends(get_database),
) -> str:
    """Generate context summary formatted for OpenClaw injection.
    
    Returns a plain text summary of active projects and tasks
    suitable for injection into Bob's context window.
    """
    # Get active projects
    projects = await db.fetch_all(
        """
        SELECT id, title, aim, description, state, created_at
        FROM projects
        WHERE state = 'active' AND deleted_at IS NULL
        ORDER BY created_at DESC
        """
    )
    
    # Get active tasks
    tasks = await db.fetch_all(
        """
        SELECT id, title, description, status, priority, requested_by
        FROM tasks
        WHERE status IN ('planning', 'active', 'pending') AND deleted_at IS NULL
        ORDER BY 
            CASE priority
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium' THEN 3
                WHEN 'low' THEN 4
                ELSE 5
            END,
            created_at DESC
        """
    )
    
    # Get upcoming events (next 7 days)
    events = await db.fetch_all(
        """
        SELECT e.id, e.title, e.start_time, e.venue, c.name as calendar_name
        FROM events e
        JOIN calendars c ON e.calendar_id = c.id
        WHERE e.start_time > datetime('now')
          AND e.start_time < datetime('now', '+7 days')
          AND e.deleted_at IS NULL
          AND e.status != 'cancelled'
        ORDER BY e.start_time
        LIMIT 10
        """
    )
    
    # Build context text
    lines = [
        "# Bob's Active Context",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    
    # Active Projects
    lines.append("## Active Projects")
    if projects:
        for p in projects:
            lines.append(f"- **{p['title']}**: {p['aim']}")
    else:
        lines.append("- No active projects")
    lines.append("")
    
    # Active Tasks
    lines.append("## Active Tasks")
    if tasks:
        for t in tasks:
            priority_emoji = {
                'critical': '🔴',
                'high': '🟠',
                'medium': '🟡',
                'low': '🟢',
            }.get(t['priority'], '⚪')
            lines.append(f"- {priority_emoji} **{t['title']}**")
            if t['requested_by']:
                lines.append(f"  Requested by: {t['requested_by']}")
    else:
        lines.append("- No active tasks")
    lines.append("")
    
    # Upcoming Events
    lines.append("## Upcoming Events (7 days)")
    if events:
        for e in events:
            start = e['start_time']
            if isinstance(start, str):
                start_str = start
            else:
                start_str = start.strftime('%Y-%m-%d %H:%M')
            venue = f" @ {e['venue']}" if e['venue'] else ""
            lines.append(f"- {start_str}: **{e['title']}**{venue}")
    else:
        lines.append("- No upcoming events")
    lines.append("")
    
    # Quick stats
    lines.append("## Summary")
    lines.append(f"- Active projects: {len(projects)}")
    lines.append(f"- Active tasks: {len(tasks)}")
    lines.append(f"- Upcoming events: {len(events)}")
    lines.append("")
    
    return "\n".join(lines)


@router.get("/context.json")
async def get_openclaw_context_json(
    request: Request,
    db: Database = Depends(get_database),
) -> dict[str, Any]:
    """Generate context summary as JSON for OpenClaw integration."""
    # Get active projects
    projects = await db.fetch_all(
        """
        SELECT id, title, aim, description, state, created_at
        FROM projects
        WHERE state = 'active' AND deleted_at IS NULL
        ORDER BY created_at DESC
        """
    )
    
    # Get active tasks
    tasks = await db.fetch_all(
        """
        SELECT id, title, description, status, priority, requested_by
        FROM tasks
        WHERE status IN ('planning', 'active', 'pending') AND deleted_at IS NULL
        ORDER BY 
            CASE priority
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium' THEN 3
                WHEN 'low' THEN 4
                ELSE 5
            END,
            created_at DESC
        """
    )
    
    # Get upcoming events
    events = await db.fetch_all(
        """
        SELECT e.id, e.title, e.start_time, e.venue, c.name as calendar_name
        FROM events e
        JOIN calendars c ON e.calendar_id = c.id
        WHERE e.start_time > datetime('now')
          AND e.start_time < datetime('now', '+7 days')
          AND e.deleted_at IS NULL
          AND e.status != 'cancelled'
        ORDER BY e.start_time
        LIMIT 10
        """
    )
    
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "projects": [dict(p) for p in projects],
        "tasks": [dict(t) for t in tasks],
        "events": [dict(e) for e in events],
        "counts": {
            "active_projects": len(projects),
            "active_tasks": len(tasks),
            "upcoming_events": len(events),
        }
    }
