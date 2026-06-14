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
import re
from pathlib import Path

from bob_server.context import AppContext
from bob_server.services.skill_env import build_skill_env
from bob_server.services.tools import ImageInjection, tool

logger = logging.getLogger(__name__)

_BASH_TIMEOUT_SECONDS = 900
_BASH_MAX_OUTPUT_CHARS = 30_000

# Sandbox guardrails. The bash tool is not chrooted — these checks catch the
# obvious escape vectors (direct DB clients, system paths, traversal). The
# system prompt carries the matching language. A determined agent can still
# evade regex filtering; stronger confinement (bubblewrap/seccomp) is the next
# layer if needed.

_DB_CLIENT_RE = re.compile(
    r"\b(sqlite3|psql|mysql|mariadb|duckdb|sqliteman|sqlitebrowser|dbeaver)\b",
    re.IGNORECASE,
)
_PRIVILEGE_RE = re.compile(r"(^|[\s;&|()`])(sudo|su|pkexec|doas)\b", re.IGNORECASE)
_DB_TOKEN_RE = re.compile(r"\b(bob\.db|bob_db_path)\b", re.IGNORECASE)
_TRAVERSAL_RE = re.compile(r"(^|[\s/\"'`=(])\.\.(?=[\s/\"'`)]|$)")
_SENSITIVE_PATH_RE = re.compile(
    r"(^|[\s\"'`=(])("
    r"/etc(?=[/\s\"'`)]|$)"
    r"|/root(?=[/\s\"'`)]|$)"
    r"|/var/lib(?=[/\s\"'`)]|$)"
    r"|/proc(?=[/\s\"'`)]|$)"
    r"|/sys(?=[/\s\"'`)]|$)"
    r"|/boot(?=[/\s\"'`)]|$)"
    r"|~/?\.ssh"
    r"|~/?\.aws"
    r"|~/?\.gnupg"
    r"|~/?\.config"
    r"|~/?\.env(?=[\s\"'`)]|$)"
    r"|\.env(?=[\s\"'`)]|$)"
    r"|credentials\.json"
    r")",
    re.IGNORECASE,
)


def _check_command_safety(command: str, *, db_path: Path | None,
                          data_dir: Path, config_dir: Path) -> str | None:
    """Return a reason string if the command violates sandbox rules, else None.

    Layered checks: DB clients, DB file references, configured sensitive paths
    (db_path / data_dir / config_dir), system/sensitive path patterns,
    path traversal, and privilege escalation.
    """
    if _DB_CLIENT_RE.search(command):
        return (
            "BLOCKED: direct database client detected. Do not query databases via bash. "
            "Use memory_*/contact_*/group_*/docs_* tools instead."
        )

    if _DB_TOKEN_RE.search(command):
        return (
            "BLOCKED: command references the bob database file or BOB_DB_PATH. "
            "The DB is off-limits via bash — use memory_*/contact_*/group_* tools."
        )

    if _PRIVILEGE_RE.search(command):
        return "BLOCKED: privilege escalation (sudo/su/pkexec/doas) is not allowed in the sandbox."

    for sensitive in (db_path, data_dir, config_dir):
        if sensitive is None:
            continue
        sp = str(sensitive)
        if not sp:
            continue
        if re.search(re.escape(sp) + r"(?=[\s\"'`)]|$|/)", command):
            return (
                f"BLOCKED: command references a path outside the workspace ({sp}). "
                "Stay inside the workspace directory; use the provided tools for data outside it."
            )

    if _SENSITIVE_PATH_RE.search(command):
        return (
            "BLOCKED: command references system paths, secrets, or config outside the workspace. "
            "Stay inside the workspace directory."
        )

    if _TRAVERSAL_RE.search(command):
        return (
            "BLOCKED: path traversal (..) is not allowed. Stay inside the workspace; "
            "use absolute workspace paths if you need to reach a subdirectory."
        )

    return None


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
        the memory index (claims, entities) will not pick them up.

        SANDBOX: The workspace is the only allowed directory. Reaching outside it
        (DB clients, /etc, /home/bob/data, /home/bob/config, ~, .., sudo, secrets)
        is blocked. Use memory_*/contact_*/group_*/docs_* tools for data outside
        the workspace — do not try to bypass blocks via subshells, python, or
        symlinks."""
        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        settings = ctx.settings
        violation = _check_command_safety(
            command,
            db_path=settings.db_path,
            data_dir=settings.data_dir,
            config_dir=settings.config_dir,
        )
        if violation:
            logger.warning("bash blocked by sandbox: %r — %s", command, violation)
            return f"Error: {violation}"
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
