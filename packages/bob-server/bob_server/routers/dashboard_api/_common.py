"""Shared helpers for the dashboard API sub-routers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from bob_server.database import Database


logger = logging.getLogger(__name__)

_STREAMABLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".pdf", ".mp4", ".webm", ".mov", ".m4v"}


def _resolve_workspace_path(settings: Any, path: str) -> Path:
    """Resolve a relative path against the workspace dir, preventing traversal."""
    workspace = settings.harness.workspace_dir.expanduser().resolve()
    resolved = (workspace / path) if path else workspace
    if ".." in Path(path).parts:
        raise ValueError(f"Path escapes workspace directory")
    return resolved


def _utc(val: str | None) -> str | None:
    if val and not val.endswith("Z") and not val.endswith("+00:00"):
        return val + "Z"
    return val


def _utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_channel(session_key: str) -> str:
    if ":voice:" in session_key or session_key.startswith("bobvoice:"):
        return "voice"
    if session_key.startswith("subagent:"):
        return "subagent"
    if ":whatsapp:" in session_key:
        return "whatsapp"
    if ":email:" in session_key:
        return "email"
    if ":phone:" in session_key:
        return "phone"
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


__all__ = [
    "json", "Path", "Any", "Request", "FileResponse", "Database",
    "logger", "_STREAMABLE_EXTENSIONS",
    "_resolve_workspace_path", "_utc", "_utc_now", "_parse_channel",
    "_check_auth", "_db",
]
