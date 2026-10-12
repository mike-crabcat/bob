"""File-based persona, rendered straight from the repo bundle.

The repo bundle (``self/<name>/{soul,identity,agents}.md``) is the single
source of truth and versioned in git (git history is the persona history).
The prompt reads it DIRECTLY (2026-10-12): the copy ``services.self_bundle``
heals into ``workspace/self/<name>/`` at boot is only a read-only view for
skills (avatar refs, clips). Reading the workspace copy let bash — which owns
those files and can chmod them back — rewrite Bob's live system prompt until
the next restart. ``user.md`` is the exception: the instance's owner profile,
read from the workspace (bundled boilerplate as fallback). The
``persona_records`` DB table is a dormant archive of the pre-file revisions
(1-10): no longer read or written.
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _bundle_base(persona: str, bundle_root: Path | None = None) -> Path:
    """The repo-bundle persona dir — the prompt's source of truth."""
    return (bundle_root or _repo_root()) / "self" / persona


def _bundled_user(bundle_root: Path | None = None) -> Path:
    return (bundle_root or _repo_root()) / _USER_FILE


def persona_source_paths(workspace_dir: Path | str,
                         *, persona: str = PERSONA_NAME,
                         bundle_root: Path | None = None) -> list[Path]:
    """Absolute paths the rendered persona reads: the bundle's persona
    files plus the workspace owner profile. Their mtimes key the workspace
    prompt cache, so a persona edit (git pull) or a user.md change
    invalidates it."""
    base = _bundle_base(persona, bundle_root)
    return ([base / name for name in _PERSONA_FILES]
            + [Path(workspace_dir).expanduser() / _USER_FILE])


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
                   *, persona: str = PERSONA_NAME,
                   bundle_root: Path | None = None) -> str:
    """Render the full persona: the three persona bodies from the repo
    bundle (``self/<name>/``) plus the instance's owner profile
    (workspace/user.md). The workspace persona copy is never read here —
    it's a skill-facing view bash can deface (see module docstring); it is
    only the fallback when the bundle itself is missing (a checkout
    without self/). ``bundle_root`` overrides the repo root (tests).
    ``{host}`` is replaced by simple substitution — not str.format — so a
    stray brace in a persona file can never crash rendering."""
    workspace = (Path(workspace_dir).expanduser() if workspace_dir
                 else Path("~/workspace").expanduser())
    bundle = _bundle_base(persona, bundle_root)
    view = workspace / "self" / persona
    identity = _read(bundle / "identity.md", view / "identity.md").replace(
        "{host}", platform.node() or "unknown-host")
    return (
        f"{_SOUL_HEADER}\n\n{_read(bundle / 'soul.md', view / 'soul.md')}\n\n"
        f"{_IDENTITY_HEADER}\n\n{identity}\n\n"
        f"{_AGENTS_HEADER}\n\n{_read(bundle / 'agents.md', view / 'agents.md')}\n\n"
        f"{_USER_HEADER}\n\n"
        f"{_read(workspace / _USER_FILE, _bundled_user(bundle_root))}"
    )


async def get_persona(db: Any = None, *,
                      workspace_dir: Path | str | None = None) -> str:
    """Render the full persona from files. ``db`` is accepted and ignored —
    the signature is kept for callers predating the file-based persona."""
    return render_persona(workspace_dir)
