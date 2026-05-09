"""Workspace file tools for LLM function calling.

Usage:
    tools = make_workspace_tools(ctx)
    result = await dispatch.chat_with_tools(messages, tools, ...)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cyborg_server.context import AppContext
from cyborg_server.services.tools import tool

logger = logging.getLogger(__name__)

_MAX_LIST_ENTRIES = 100
_MAX_READ_BYTES = 50 * 1024
_MAX_WRITE_BYTES = 100 * 1024


def _resolve_path(ctx: AppContext, path: str) -> Path:
    """Resolve a relative path against the workspace dir, preventing traversal."""
    workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
    resolved = (workspace / path).resolve()
    if not str(resolved).startswith(str(workspace)):
        raise ValueError(f"Path '{path}' escapes workspace directory")
    return resolved


def make_workspace_tools(ctx: AppContext):
    """Create workspace file tools bound to the given context."""

    @tool
    async def list_files(
        path: str = "",
        depth: int = 1,
    ) -> str:
        """List files and directories in the workspace. Depth controls recursion (1=immediate, 2=one level deeper)."""
        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        target = _resolve_path(ctx, path) if path else workspace
        if not target.is_dir():
            return json.dumps({"error": f"'{path}' is not a directory"})

        entries: list[dict] = []

        def _walk(dir_path: Path, current_depth: int) -> None:
            if len(entries) >= _MAX_LIST_ENTRIES:
                return
            try:
                children = sorted(dir_path.iterdir())
            except PermissionError:
                return
            for child in children:
                if len(entries) >= _MAX_LIST_ENTRIES:
                    entries.append({"name": "...", "type": "truncated"})
                    return
                rel = str(child.relative_to(workspace))
                entry: dict = {
                    "name": rel,
                    "type": "dir" if child.is_dir() else "file",
                }
                if child.is_file():
                    try:
                        entry["size_bytes"] = child.stat().st_size
                    except OSError:
                        pass
                entries.append(entry)
                if child.is_dir() and current_depth < depth:
                    _walk(child, current_depth + 1)

        _walk(target, 1)
        return json.dumps(entries)

    @tool
    async def read_file(
        path: str,
    ) -> str:
        """Read the contents of a file in the workspace."""
        resolved = _resolve_path(ctx, path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file"

        size = resolved.stat().st_size
        if size > _MAX_READ_BYTES:
            return f"Error: file is {size} bytes (max {_MAX_READ_BYTES})"

        content = resolved.read_bytes()
        if b"\x00" in content[:8192]:
            return "Error: binary file"

        return content.decode("utf-8")

    @tool
    async def write_file(
        path: str,
        content: str,
    ) -> str:
        """Write content to a file in the workspace. Creates parent directories if needed."""
        if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            return f"Error: content exceeds {_MAX_WRITE_BYTES} bytes"

        resolved = _resolve_path(ctx, path)
        if resolved.exists() and resolved.is_dir():
            return f"Error: '{path}' is a directory"

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        logger.info("Workspace write: %s (%d bytes)", path, len(content))
        return json.dumps({"ok": True, "path": path, "bytes": len(content.encode("utf-8"))})

    return [list_files, read_file, write_file]
