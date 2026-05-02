"""Shared helpers for the dashboard sub-routers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from cyborg_server.config import Settings
from cyborg_server.database import Database


def _get_settings() -> Settings:
    return Settings.from_env()


async def _get_pending_approval_count(db: Database) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) as count FROM approvals WHERE status = 'pending'",
    )
    return int(row["count"]) if row and row["count"] else 0


async def _get_project_id_for_task(db: Database, task_id: str) -> str | None:
    row = await db.fetch_one("SELECT project_id FROM project_tasks WHERE task_id = ?", (task_id,))
    return row["project_id"] if row else None


def _approval_review_href(approval_type: str | None, entity_id: str | None, metadata: dict | None = None) -> str | None:
    if not entity_id:
        return None
    if approval_type in {"project_plan", "strategy_refinement", "follow_up_tasks"}:
        return f"/dashboard/projects/{entity_id}"
    if approval_type == "task_input":
        if metadata and metadata.get("entity_kind") == "project":
            return f"/dashboard/projects/{entity_id}"
        return None
    return None


def _render_template(template_name: str, request: Request, context: dict[str, Any]) -> HTMLResponse:
    from jinja2 import Environment, FileSystemLoader

    templates_dir = Path(__file__).parent.parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)))

    env.filters['relative_time'] = _format_relative_time
    env.filters['file_size'] = _format_file_size
    env.filters['file_icon'] = _file_icon
    env.filters['unescape_newlines'] = lambda v: v.replace('\\n', '\n') if isinstance(v, str) else v

    context["request"] = request

    settings = _get_settings()
    context.setdefault("version", settings.version)
    context.setdefault("pending_count", 0)

    template = env.get_template(template_name)
    response = HTMLResponse(content=template.render(context))

    if settings.dashboard_secret_configured:
        response.set_cookie(
            key="cyborg_dashboard_secret",
            value=settings.dashboard_secret,
            httponly=True,
            samesite="strict",
        )

    return response


def _format_time(iso_string: str | None) -> str:
    if not iso_string:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "--:--:--"


def _format_relative_time(iso_string: str | None) -> str:
    if not iso_string:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        if delta.days > 0:
            return f"{delta.days}d ago"
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return f"{hours}h ago"
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return f"{minutes}m ago"
        else:
            return "just now"
    except (ValueError, TypeError):
        return "unknown"


def _format_duration_minutes(avg_minutes: float | None) -> str:
    if avg_minutes is None:
        return "--"
    total_minutes = int(round(avg_minutes))
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def _format_file_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "--"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _file_icon(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    icons = {
        "md": "\U0001f4c4", "txt": "\U0001f4c4",
        "png": "\U0001f5bc", "jpg": "\U0001f5bc", "jpeg": "\U0001f5bc",
        "webp": "\U0001f5bc", "gif": "\U0001f5bc",
        "mp3": "\U0001f3b5", "wav": "\U0001f3b5", "flac": "\U0001f3b5",
        "py": "\U0001f40d", "sh": "⚙",
        "json": "\U0001f4cb", "csv": "\U0001f4ca",
        "pdf": "\U0001f4d5",
    }
    return icons.get(ext, "\U0001f4ce")


_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"}


def _scan_project_files(
    project_path: Path,
    *,
    task_id_map: dict[str, str] | None = None,
    task_file_limit: int = 0,
    category_file_limit: int = 0,
) -> list[dict[str, Any]]:
    workspace_root = project_path.resolve()
    if not workspace_root.exists():
        return []

    files: list[dict[str, Any]] = []
    task_file_counts: dict[str, int] = {}
    cat_file_counts: dict[str, int] = {}
    for child in sorted(workspace_root.rglob("*")):
        if not child.is_file():
            continue
        rel_parts = child.relative_to(workspace_root).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts):
            continue

        relative = str(child.relative_to(workspace_root))
        stat = child.stat()
        category = rel_parts[0] if len(rel_parts) > 1 else "root"

        entry: dict[str, Any] = {
            "name": child.name,
            "relative_path": relative,
            "category": category,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

        is_task_file = False
        if category == "tasks" and len(rel_parts) >= 3 and task_id_map:
            short_id = rel_parts[1]
            if short_id in task_id_map:
                entry["task_short_id"] = short_id
                entry["task_title"] = task_id_map[short_id]
                is_task_file = True

                if task_file_limit > 0:
                    task_file_counts[short_id] = task_file_counts.get(short_id, 0) + 1
                    if task_file_counts[short_id] > task_file_limit:
                        continue

        if not is_task_file and category_file_limit > 0:
            cat_file_counts[category] = cat_file_counts.get(category, 0) + 1
            if cat_file_counts[category] > category_file_limit:
                continue

        files.append(entry)

    if task_file_limit > 0:
        for f in files:
            tid = f.get("task_short_id")
            if tid and tid in task_file_counts:
                f["task_file_count"] = task_file_counts[tid]
    if category_file_limit > 0:
        for f in files:
            cat = f.get("category")
            if cat and cat in cat_file_counts and "task_short_id" not in f:
                f["category_file_count"] = cat_file_counts[cat]

    return files


def _group_files_by_category(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        categories.setdefault(f["category"], []).append(f)
    return categories
