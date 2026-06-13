"""Workspace tools for LLM function calling.

Usage:
    tools = make_workspace_tools(ctx)
    result = await dispatch.chat_with_tools(messages, tools, ...)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

from bob_server.context import AppContext
from bob_server.services.skill_env import build_skill_env
from bob_server.services.tools import ImageInjection, tool

logger = logging.getLogger(__name__)

_BASH_TIMEOUT_SECONDS = 900
_BASH_MAX_OUTPUT_CHARS = 30_000


def _resolve_path(ctx: AppContext, path: str) -> Path:
    """Resolve a path against the workspace dir.

    Accepts both absolute and workspace-relative paths:
    - Absolute path: must be within the workspace, returned as-is after validation.
    - Relative path: resolved against workspace root (backward compatible).
    """
    workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
    parsed = Path(path)
    if ".." in parsed.parts:
        raise ValueError(f"Path '{path}' escapes workspace directory")
    if parsed.is_absolute():
        resolved = parsed.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            raise ValueError(f"Path '{path}' is outside the workspace directory")
        return resolved
    return workspace / path


def make_workspace_tools(ctx: AppContext, *, session_key: str | None = None):
    """Create workspace tools bound to the given context.

    If session_key is provided, also includes an update_agenda tool.
    """

    @tool
    async def bash(
        command: str,
    ) -> str:
        """Run a bash command in the workspace directory. The workspace is the cwd, so
        relative paths land there. The skill environment (BOB_* vars) is inherited.
        Output above 30000 chars is truncated — use head/tail/sed -n/grep to page
        through large files. Times out after 900s.

        Do NOT write files under memory/ with this tool — use memory_write instead, or
        the memory index (claims, entities) will not pick them up."""
        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        logger.info("bash: %s", command)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_skill_env(workspace_dir=str(workspace)),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_BASH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: command timed out after {_BASH_TIMEOUT_SECONDS}s"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            body = err or out
            prefix = f"Error (exit code {proc.returncode}):\n"
        else:
            body = out
            prefix = ""

        if len(body) > _BASH_MAX_OUTPUT_CHARS:
            body = body[:_BASH_MAX_OUTPUT_CHARS] + (
                f"\n... output truncated ({_BASH_MAX_OUTPUT_CHARS}/{len(body)} chars) "
                f"— use head/tail/sed -n/grep to page through"
            )
        return prefix + body

    @tool
    async def read_image(
        path: str,
    ) -> ImageInjection:
        """Load an image from the workspace so you can see and analyze it. Supports PNG, JPG, GIF, WebP, and BMP. Path can be absolute (within workspace) or relative to workspace root."""
        resolved = _resolve_path(ctx, path)
        if not resolved.is_file():
            return ImageInjection(text=f"Error: '{path}' is not a file", data_url="")

        suffix = resolved.suffix.lower()
        _MIME_MAP = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }
        mime = _MIME_MAP.get(suffix)
        if not mime:
            return ImageInjection(text=f"Error: '{path}' is not a supported image format (use png, jpg, gif, webp, or bmp)", data_url="")

        data = resolved.read_bytes()
        b64 = base64.b64encode(data).decode()
        return ImageInjection(
            text=f"Image loaded from {path} ({len(data)} bytes)",
            data_url=f"data:{mime};base64,{b64}",
        )

    @tool
    async def use_skill(
        skill_name: str,
    ) -> str:
        """Load the full instructions for a skill by name. Returns the skill's instructions
        and the path to its directory so you can run its scripts via bash with correct paths."""
        from bob_server.services.skill_loader import load_skill
        return load_skill(ctx.settings.harness.workspace_dir, skill_name)

    tools = [bash, read_image, use_skill]

    if session_key:
        @tool
        async def update_agenda(agenda: str) -> str:
            """Update the agenda for this session. The agenda extends the system prompt,
            guiding your behavior for all subsequent turns. Replace the full agenda text —
            use this to mark tasks complete, add new goals, or change your instructions."""
            from bob_server.services.session_agenda_service import SessionAgendaService
            await SessionAgendaService(ctx).set_agenda(session_key, agenda)
            return json.dumps({"ok": True})

        tools.append(update_agenda)

    return tools
