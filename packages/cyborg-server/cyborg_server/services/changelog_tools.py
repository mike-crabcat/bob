"""Changelog tools for LLM function calling.

Usage:
    tools.extend(make_changelog_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from cyborg_server.context import AppContext
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)

# Resolve once at import time: walk up from this file to find CHANGELOG.md
_PACKAGE_DIR = Path(__file__).resolve().parent  # .../cyborg_server/services
_SERVER_DIR = _PACKAGE_DIR.parent  # .../cyborg_server
_CHANGELOG_PATH: Path | None = None
for parent in _SERVER_DIR.parents:
    candidate = parent / "CHANGELOG.md"
    if candidate.exists():
        _CHANGELOG_PATH = candidate
        break
if _CHANGELOG_PATH is None:
    # Also check the package directory itself (installed editable)
    candidate = _SERVER_DIR / "CHANGELOG.md"
    if candidate.exists():
        _CHANGELOG_PATH = candidate

_REPO_ROOT = _CHANGELOG_PATH.parent if _CHANGELOG_PATH else None


def make_changelog_tools(ctx: AppContext, *, session_key: str = "") -> list[Tool]:
    """Create changelog tool bound to the given context."""

    @tool
    async def read_changelog(
        last: int = 3,
        unit: str = "commits",
    ) -> str:
        """Read the Cyborg project changelog to find out what changed recently. Use this when the user asks what's new, what changed, what was added/fixed recently, or when you need context about recent Cyborg project history before answering a question. Returns changelog entries filtered by the last N days or last N commits. Default: last 3 commits. Set unit to "days" to filter by date range instead."""
        if _CHANGELOG_PATH is None:
            return "No CHANGELOG.md found"

        content = _CHANGELOG_PATH.read_text(encoding="utf-8")

        if unit == "days":
            return _filter_by_days(content, last)
        elif _REPO_ROOT is not None:
            return _filter_by_commits(_REPO_ROOT, content, last)
        else:
            return content[:4000]

    return [read_changelog]


def _filter_by_days(content: str, days: int) -> str:
    """Return changelog sections dated within the last N days."""
    cutoff = datetime.now() - timedelta(days=days)

    lines = content.split("\n")
    result: list[str] = []
    capturing = False
    date_header_count = 0

    for line in lines:
        if line.startswith("## ") and not line.startswith("## ["):
            date_str = line.lstrip("# ").strip()
            try:
                header_date = datetime.strptime(date_str, "%Y-%m-%d")
                if header_date.date() >= cutoff.date():
                    capturing = True
                    date_header_count += 1
                    result.append(line)
                else:
                    capturing = False
            except ValueError:
                capturing = True
                result.append(line)
        elif line.startswith("## ["):
            capturing = True
            result.append(line)
        elif line.startswith("# "):
            result.append(line)
        elif capturing:
            result.append(line)

    output = "\n".join(result).strip()
    if not output or date_header_count == 0:
        return f"No changelog entries found in the last {days} days"
    return output


def _filter_by_commits(workspace: Path, content: str, commits: int) -> str:
    """Return changelog sections that correspond to the last N commits."""
    try:
        since_date = subprocess.run(
            ["git", "log", f"-{commits}", "--format=%ai", "--reverse"],
            capture_output=True,
            text=True,
            cwd=str(workspace),
        )
        if since_date.returncode != 0 or not since_date.stdout.strip():
            return content[:4000]

        first_line = since_date.stdout.strip().split("\n")[0]
        cutoff = datetime.strptime(first_line[:10], "%Y-%m-%d").date()
    except Exception:
        return content[:4000]

    lines = content.split("\n")
    result: list[str] = []
    capturing = False

    for line in lines:
        if line.startswith("## ") and not line.startswith("## ["):
            date_str = line.lstrip("# ").strip()
            try:
                header_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                capturing = header_date >= cutoff
                if capturing:
                    result.append(line)
            except ValueError:
                capturing = True
                result.append(line)
        elif line.startswith("## ["):
            capturing = True
            result.append(line)
        elif line.startswith("# "):
            result.append(line)
        elif capturing:
            result.append(line)

    output = "\n".join(result).strip()
    return output if output else content[:4000]
