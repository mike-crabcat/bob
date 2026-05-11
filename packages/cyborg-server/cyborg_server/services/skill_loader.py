"""Discover and load skills from workspace/skills/ into the system prompt."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache keyed by mtime hash.
_skills_cache: tuple[Any, str] | None = None


def load_skills_prompt(workspace_dir: Path) -> str:
    """Scan skills/ directory, parse skill.md frontmatter, return combined prompt."""
    global _skills_cache

    workspace_dir = workspace_dir.expanduser()
    skills_dir = workspace_dir / "skills"

    if not skills_dir.is_dir():
        return ""

    # Build mtime hash from all skill.md files
    mtimes: dict[str, float] = {}
    for skill_path in sorted(skills_dir.iterdir()):
        md = skill_path / "skill.md"
        if skill_path.is_dir() and md.is_file():
            mtimes[skill_path.name] = md.stat().st_mtime

    if not mtimes:
        return ""

    mtime_hash = tuple(sorted(mtimes.items()))
    if _skills_cache is not None and _skills_cache[0] == mtime_hash:
        return _skills_cache[1]

    parts: list[str] = []
    for skill_name in sorted(mtimes):
        md = skills_dir / skill_name / "skill.md"
        content = md.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)

    if not parts:
        return ""

    combined = "\n\n---\n\n".join(parts)
    _skills_cache = (mtime_hash, combined)
    logger.info(
        "Skills loaded: dir=%s count=%d chars=%d",
        skills_dir, len(parts), len(combined),
    )
    return combined
