"""Structured file tools: read_file, edit_file, multi_edit (2026-10-12).

Bob edited existing files through bash heredocs and sed — quoting
breakage, whole-file clobbers, no record of what changed — and read big
files through bash's 30k truncation. These give him paged, numbered reads
and exact-match edits that return a unified diff of what they did.

Path policy is shared with the other workspace tools (``_resolve_path`` /
``resolve_writable`` in ``workspace_tools``): workspace-only, resolved
before the containment check (symlinks can't escape), and the persona view
(``self/``) is never writable. Kill switch: ``BOB_FILE_TOOLS=off``.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Any

from server.context import AppContext
from server.services.tools import Tool, tool

READ_DEFAULT_LIMIT = 400
READ_MAX_LIMIT = 2000
LINE_MAX_CHARS = 2000
READ_MAX_CHARS = 30_000
DIFF_MAX_CHARS = 3000

_MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp4", ".m4v", ".mov", ".webm", ".mpeg", ".mpg",
}


def file_tools_enabled() -> bool:
    return os.getenv("BOB_FILE_TOOLS", "on").strip().lower() not in (
        "0", "false", "no", "off")


def _read_text(path: Path, shown: str) -> str:
    """Raw text (line endings preserved). Raises ValueError for non-text."""
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ValueError(f"'{shown}' is a binary file — not editable as text")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"'{shown}' is not UTF-8 text — not editable as text")


def _atomic_write(path: Path, text: str) -> None:
    """Temp file + rename in the same directory; keeps the file's mode (the
    exec bit on skill scripts survives an edit)."""
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _match_lines(text: str, needle: str) -> list[int]:
    lines, start = [], 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return lines
        lines.append(_line_of(text, i))
        start = i + max(1, len(needle))


def _closest_lines(text: str, needle: str, n: int = 3) -> str:
    """Where old_string probably meant — the lines most like its first
    non-blank line, with numbers, so the model can re-read and retry."""
    probe = next((ln.strip() for ln in needle.splitlines() if ln.strip()), "")
    if not probe:
        return ""
    lines = text.splitlines()
    scored = sorted(
        ((difflib.SequenceMatcher(None, probe, ln.strip()).ratio(), i)
         for i, ln in enumerate(lines) if ln.strip()),
        reverse=True)[:n]
    picked = [(i, lines[i]) for score, i in scored if score >= 0.5]
    if not picked:
        return ""
    return "Closest lines:\n" + "\n".join(
        f"{i + 1:>6}\t{ln[:200]}" for i, ln in sorted(picked))


def _apply_edit(text: str, old: str, new: str, replace_all: bool,
                label: str = "") -> tuple[str, str | None]:
    """One str_replace against ``text``. Returns (new_text, error)."""
    if not old:
        return text, (f"{label}old_string is empty — give the exact text to "
                      "replace (to create a new file, use bash)")
    if old == new:
        return text, f"{label}old_string and new_string are identical — nothing to change"
    hits = _match_lines(text, old)
    if not hits:
        hint = _closest_lines(text, old)
        return text, (f"{label}old_string not found (it must match exactly, "
                      "including whitespace and indentation). Re-read the "
                      "file with read_file and copy the text verbatim."
                      + (f"\n{hint}" if hint else ""))
    if len(hits) > 1 and not replace_all:
        shown = ", ".join(str(n) for n in hits[:10])
        return text, (f"{label}old_string matches {len(hits)} places (lines "
                      f"{shown}) — include more surrounding lines to make it "
                      "unique, or pass replace_all=true to change every one")
    return (text.replace(old, new) if replace_all
            else text.replace(old, new, 1)), None


def _diff(before: str, after: str, shown: str) -> str:
    out = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{shown}", tofile=f"b/{shown}", n=2))
    if len(out) > DIFF_MAX_CHARS:
        out = out[:DIFF_MAX_CHARS] + "\n… [diff truncated]"
    return out


def make_file_tools(ctx: AppContext) -> list[Tool]:
    from server.services.workspace_tools import _resolve_path, resolve_writable

    @tool
    async def read_file(path: str, offset: int = 1, limit: int = READ_DEFAULT_LIMIT) -> str:
        """Read a workspace text file with line numbers (cat -n style), a page at a time. Use this — not cat — for any file you're about to edit (edit_file needs the exact text) and for big files: the header gives the total line count; pass offset to read further. Find a spot first with `rg -n pattern file` in bash, then read_file around that line. Path is absolute (inside the workspace) or workspace-relative. Images/videos: use read_image / read_video."""
        try:
            resolved = _resolve_path(ctx, path)
        except ValueError as e:
            return f"Error: {e}"
        if not resolved.is_file():
            return f"Error: '{path}' is not a file"
        if resolved.suffix.lower() in _MEDIA_SUFFIXES:
            return (f"'{path}' is media — use read_image (images, video first "
                    "frame) or read_video to view it")
        try:
            text = _read_text(resolved, path)
        except ValueError as e:
            return f"Error: {e}"
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return f"{path} — empty file"
        start = max(1, int(offset))
        if start > total:
            return f"{path} — {total} lines; offset {start} is past the end"
        limit = min(max(1, int(limit)), READ_MAX_LIMIT)
        out, used, end = [], 0, start - 1
        for n in range(start, min(total, start + limit - 1) + 1):
            line = lines[n - 1]
            if len(line) > LINE_MAX_CHARS:
                line = line[:LINE_MAX_CHARS] + " … [line truncated]"
            row = f"{n:>6}\t{line}"
            if used + len(row) > READ_MAX_CHARS and out:
                break
            out.append(row)
            used += len(row) + 1
            end = n
        header = f"{path} — {total} lines; showing {start}-{end}."
        if end < total:
            header += f" Use offset={end + 1} to continue."
        return header + "\n" + "\n".join(out)

    @tool
    async def edit_file(path: str, old_string: str, new_string: str,
                        replace_all: bool = False) -> str:
        """Change an existing workspace file by exact text replacement — use this instead of sed or rewriting the whole file with a heredoc. old_string must match the file exactly (whitespace and indentation included) and be unique; include a few surrounding lines if the text repeats, or set replace_all=true to change every occurrence. Copy old_string from read_file output (without the line-number prefix). Returns the unified diff of what changed. For several changes to one file, use multi_edit. The persona view (self/) is read-only."""
        try:
            resolved = resolve_writable(ctx, path)
        except ValueError as e:
            return f"Error: {e}"
        if not resolved.is_file():
            return f"Error: '{path}' is not an existing file (to create one, use bash)"
        try:
            before = _read_text(resolved, path)
        except ValueError as e:
            return f"Error: {e}"
        after, err = _apply_edit(before, old_string, new_string, replace_all)
        if err:
            return f"Error: {err}"
        _atomic_write(resolved, after)
        return f"Edited {path}.\n{_diff(before, after, path)}"

    @tool
    async def multi_edit(path: str, edits: list[dict]) -> str:
        """Apply several exact-text replacements to ONE workspace file in a single call, in order, all-or-nothing: if any edit fails, the file is left untouched and the error says which edit and why. Each edit is {"old_string": ..., "new_string": ..., "replace_all": false}; later edits see the result of earlier ones. Same matching rules as edit_file. Returns one combined diff."""
        try:
            resolved = resolve_writable(ctx, path)
        except ValueError as e:
            return f"Error: {e}"
        if not resolved.is_file():
            return f"Error: '{path}' is not an existing file (to create one, use bash)"
        if not isinstance(edits, list) or not edits:
            return "Error: edits must be a non-empty list of {old_string, new_string}"
        try:
            before = _read_text(resolved, path)
        except ValueError as e:
            return f"Error: {e}"
        text = before
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict):
                return f"Error: edit {i} is not an object — nothing written"
            text, err = _apply_edit(
                text, str(e.get("old_string") or ""), str(e.get("new_string") or ""),
                bool(e.get("replace_all")), label=f"edit {i}: ")
            if err:
                return f"Error: {err} — nothing written"
        _atomic_write(resolved, text)
        return f"Edited {path} ({len(edits)} edits).\n{_diff(before, text, path)}"

    # The generic schema builder has no object type for list[dict] items —
    # spell the edit shape out so the model sees the fields.
    multi_edit.parameters["edits"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["old_string", "new_string"],
        },
    }
    return [read_file, edit_file, multi_edit]


__all__: list[Any] = ["make_file_tools", "file_tools_enabled"]
