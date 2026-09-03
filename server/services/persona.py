"""File-based persona, rendered from the healed workspace copies.

The repo bundle (``self/<name>/{soul,identity,agents}.md`` plus ``user.md``
at the workspace root) is the single source of truth, mirrored into the
workspace at every boot by ``services.self_bundle`` — so the files this
module reads are corruption-proof upstream and versioned in git (git
history is the persona history). The ``persona_records`` DB table is a
dormant archive of the pre-file revisions (1-10): no longer read or written.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# self/<name>/ — multi-persona ready; a settings key can select others later.
PERSONA_NAME = "bob"

_PERSONA_FILES = ("soul.md", "identity.md", "agents.md")
_USER_FILE = "user.md"

# --- Framing headers (hardcoded, never user-editable) ---

_SOUL_HEADER = """\
# Your Soul

This defines who you are at your core — your personality, communication style, and behavioral rules."""

_IDENTITY_HEADER = """\
# Your Identity

Who you are, what you do, and how you work."""

_AGENTS_HEADER = """\
# Your Agents

Behavioral guardrails for tool use, group chats, and external actions."""

_USER_HEADER = """\
# Your User

Everything you know about the person you're helping. Learn and update as you go."""


def _bundle_base(persona: str) -> Path:
    """The repo-bundle location (last-resort fallback when the workspace
    copy is missing — e.g. the heal has not run yet)."""
    return Path(__file__).resolve().parents[2] / "self" / persona


def _bundled_user() -> Path:
    return Path(__file__).resolve().parents[2] / _USER_FILE


def persona_file_paths() -> list[str]:
    """Workspace-relative persona paths. Their mtimes key the workspace
    prompt cache, so a healed-in persona change invalidates the cache."""
    return [f"self/{PERSONA_NAME}/{name}" for name in _PERSONA_FILES] + [_USER_FILE]


def _read(path: Path, fallback: Path | None = None) -> str:
    for p in (path, fallback):
        if p is None:
            continue
        try:
            content = p.read_text(encoding="utf-8").strip()
            if content:
                return content
        except OSError:
            continue
        if p is fallback:
            break
    logger.warning("persona file unreadable: %s", path)
    return ""


def render_persona(workspace_dir: Path | str | None = None,
                   *, persona: str = PERSONA_NAME) -> str:
    """Render the full persona: the three persona bodies from
    workspace/self/<name>/ plus the instance's owner profile
    (workspace/user.md). ``{host}`` is replaced by simple substitution —
    not str.format — so a stray brace in a persona file can never crash
    rendering."""
    workspace = (Path(workspace_dir).expanduser() if workspace_dir
                 else Path("~/workspace").expanduser())
    base = workspace / "self" / persona
    bundle = _bundle_base(persona)
    identity = _read(base / "identity.md", bundle / "identity.md").replace(
        "{host}", platform.node() or "unknown-host")
    return (
        f"{_SOUL_HEADER}\n\n{_read(base / 'soul.md', bundle / 'soul.md')}\n\n"
        f"{_IDENTITY_HEADER}\n\n{identity}\n\n"
        f"{_AGENTS_HEADER}\n\n{_read(base / 'agents.md', bundle / 'agents.md')}\n\n"
        f"{_USER_HEADER}\n\n{_read(workspace / _USER_FILE, _bundled_user())}"
    )


async def get_persona(db: Any = None, *,
                      workspace_dir: Path | str | None = None) -> str:
    """Render the full persona from files. ``db`` is accepted and ignored —
    the signature is kept for callers predating the file-based persona."""
    return render_persona(workspace_dir)
