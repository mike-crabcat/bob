"""Dashboard API: Workspace file browsing and editing."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/workspace")
async def list_workspace(request: Request, path: str = "", depth: int = 1) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    try:
        target = _resolve_workspace_path(settings, path)
    except ValueError:
        return {"error": "invalid path"}
    if not target.is_dir():
        return {"error": "not a directory"}

    workspace = settings.harness.workspace_dir.expanduser().resolve()
    entries: list[dict[str, Any]] = []

    def _walk(dir_path: Path, prefix: str, current_depth: int) -> None:
        if len(entries) >= 200:
            return
        try:
            children = sorted(dir_path.iterdir())
        except PermissionError:
            return
        for child in children:
            if len(entries) >= 200:
                break
            name = f"{prefix}{child.name}" if prefix else child.name
            entry: dict[str, Any] = {"name": name, "type": "dir" if child.is_dir() else "file"}
            if child.is_file():
                try:
                    entry["size_bytes"] = child.stat().st_size
                except OSError:
                    pass
            entries.append(entry)
            if child.is_dir() and current_depth < depth:
                _walk(child, f"{name}/", current_depth + 1)

    prefix = f"{path}/" if path else ""
    _walk(target, prefix, 1)
    return {"entries": entries, "path": path, "root": str(workspace)}


@router.get("/api/workspace/file")
async def read_workspace_file(request: Request, path: str = "", download: int = 0) -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    if not path:
        return {"error": "path required"}
    settings = request.app.state.settings
    try:
        resolved = _resolve_workspace_path(settings, path)
    except ValueError:
        return {"error": "invalid path"}
    if not resolved.is_file():
        return {"error": "not a file"}

    size = resolved.stat().st_size
    suffix = resolved.suffix.lower()

    # Images/videos/PDFs: stream directly via FileResponse
    if suffix in _STREAMABLE_EXTENSIONS:
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
            ".bmp": "image/bmp", ".ico": "image/x-icon", ".pdf": "application/pdf",
            ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
            ".m4v": "video/x-m4v",
        }
        content_type = mime_map.get(suffix, "application/octet-stream")
        headers: dict[str, str] | None = None
        if download:
            filename = resolved.name
            headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return FileResponse(resolved, media_type=content_type, headers=headers)

    # Text files
    data = resolved.read_bytes()
    # Count null bytes — a few may indicate encoding corruption, many means binary
    null_count = data[:8192].count(b"\x00")
    if null_count > 5:
        return {"type": "binary", "size_bytes": size, "path": path}

    return {"type": "text", "content": data.decode("utf-8", errors="replace"), "path": path, "size_bytes": size}


@router.put("/api/workspace/file")
async def write_workspace_file(request: Request, path: str = "") -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    if not path:
        return {"error": "path required"}
    settings = request.app.state.settings
    try:
        resolved = _resolve_workspace_path(settings, path)
    except ValueError:
        return {"error": "invalid path"}

    body = await request.json()
    content = body.get("content", "")
    if len(content.encode("utf-8")) > 200 * 1024:
        return {"error": "content too large (max 200KB)"}

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    logger.info("Dashboard workspace write: %s (%d bytes)", path, len(content))
    return {"ok": True}


# ── Memory ──────────────────────────────────────────────────────────────────


