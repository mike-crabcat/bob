"""Compact self-knowledge brief appended to the system prompt.

The 2026-09-14 self-memory review found self-bob's claims reachable only
via explicit recall — Bob's runtime self-model was the static persona
bundle, so even its good self-knowledge never influenced turns unless Bob
went looking for it. This module renders a whitelisted, capped subset of
self-bob's active claims as a block appended to the END of the system
prompt (the base prompt stays byte-stable for prompt caching; only this
tail re-prices when self-knowledge changes).

Junk discipline (the review also found ~66% of limit rows were incident
stories and tool how-tos): the whitelist is closed — self_state,
self_image, practice, limit only. capability/milestone/incident/feedback
stay recall-only. Per-section row caps and first-sentence truncation for
the long-form types keep the block under ~4k chars (~1k tokens).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SELF_ID = "self-bob"

# (claim_type_key, max rows, first-sentence only, section title)
_SECTIONS: list[tuple[str, int, bool, str]] = [
    ("self_state", 8, False, "Current state"),
    ("self_image", 2, False, "Self-image"),
    ("practice", 15, True, "Standing practices (learned rules)"),
    ("limit", 15, True, "Known limits"),
]

_MAX_TOTAL_CHARS = 4000
_TRUNC_CHARS = 220


def _first_sentence(text: str, *, hard_limit: int = _TRUNC_CHARS) -> str:
    """The rule, not the story: cut at the first sentence boundary, an
    em-dash aside, or a hard character cap (whichever comes first). The
    incident-shaped claims this exists for read 'rule — dated story', so
    the em-dash is the most reliable cut point in practice."""
    text = " ".join(text.split())
    if len(text) <= hard_limit:
        return text
    cut = text[:hard_limit]
    for sep in (". ", "; ", " — "):
        idx = cut.rfind(sep)
        if idx > 40:
            return cut[:idx].rstrip(",;:—- ") + ("." if sep == ". " else "")
    # No clean boundary — cut at the last space so we don't mid-word.
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + " …"


# Module cache: re-render only when self-bob's active claim set changes.
# The probe (count + max created_at) is one indexed query per prompt build.
_cache_key: tuple[int, str] | None = None
_cache_text: str = ""


async def _change_key(db: Any) -> tuple[int, str]:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), '') AS mx "
        "FROM memory_claims WHERE subject_id = ? AND status = 'active'",
        (SELF_ID,),
    )
    return (int(row["n"]) if row else 0, (row["mx"] if row else "") or "")


async def render_self_brief(db: Any) -> str:
    """Render the self-brief block (empty string when there is nothing to
    show). Cached against the active-claim change key."""
    global _cache_key, _cache_text

    key = await _change_key(db)
    if key == _cache_key:
        return _cache_text
    if key[0] == 0:
        _cache_key, _cache_text = key, ""
        return ""

    lines: list[str] = []
    for type_key, cap, truncate, title in _SECTIONS:
        rows = await db.fetch_all(
            "SELECT value FROM memory_claims "
            "WHERE subject_id = ? AND claim_type_key = ? AND status = 'active' "
            "AND COALESCE(value, '') != '' ORDER BY created_at DESC LIMIT ?",
            (SELF_ID, type_key, cap + 1),  # +1 to detect overflow
        )
        if not rows:
            continue
        overflow = len(rows) > cap
        lines.append(f"{title}:")
        for r in rows[:cap]:
            v = r["value"] or ""
            lines.append(f"- {_first_sentence(v) if truncate else ' '.join(v.split())}")
        if overflow:
            lines.append(f"- …{len(rows) - cap} more — use recall('self-bob')")

    if not lines:
        _cache_key, _cache_text = key, ""
        return ""

    block = (
        "## Self-Brief — your current self-model from memory\n"
        "(live view of your self-bob claims; treat as current truth about "
        "yourself)\n" + "\n".join(lines)
    )
    if len(block) > _MAX_TOTAL_CHARS:
        block = block[: _MAX_TOTAL_CHARS].rsplit("\n", 1)[0] + "\n…(truncated)"
    _cache_key, _cache_text = key, block
    return block


def reset_cache() -> None:
    """Test hook — the module cache must not leak between test DBs."""
    global _cache_key, _cache_text
    _cache_key, _cache_text = None, ""


async def self_brief_block(db: Any) -> str:
    """Prompt-suffix entry point: honour the BOB_SELF_BRIEF kill switch and
    never let a render failure break the prompt."""
    import os
    if os.getenv("BOB_SELF_BRIEF", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""
    try:
        return await render_self_brief(db)
    except Exception:
        logger.warning("self-brief render failed", exc_info=True)
        return ""
