"""Self-memory vocabulary + self-brief tests (2026-09-14 self-memory review).

Covers: the four new self claim types (migration 006 / registry), single-active
per-facet semantics for self_state in write_claim, and the prompt-injected
self-brief (whitelist, caps, truncation, kill switch, change-detection cache).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from server.services.memory.models import Claim
from server.services.memory.claim_service import write_claim


async def _seed_self(db) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO memory_entities "
        "(entity_id, entity_type, display_name, status, created_at) "
        "VALUES ('self-bob', 'self', 'Bob', 'active', datetime('now'))")


async def _claim(claim_id: str, type_key: str, value: str) -> Claim:
    return Claim(id=claim_id, claim_type_key=type_key, subject_id="self-bob",
                 value=value, status="active", created_at=datetime.now())


@pytest.fixture(autouse=True)
def _reset_brief_cache():
    from server.services.memory import self_brief
    self_brief.reset_cache()
    yield
    self_brief.reset_cache()


# ---------------------------------------------------------------------------
# new claim types
# ---------------------------------------------------------------------------

async def test_new_self_types_registered(db):
    from server.services.memory.claim_types import get_claim_types_for_entity
    keys = {ct.key for ct in get_claim_types_for_entity("self")}
    assert {"self_state", "practice", "feedback", "incident"} <= keys


async def test_extraction_prompt_mentions_new_types(db):
    from server.services.memory.claim_types import build_extraction_prompt_section
    section = build_extraction_prompt_section(["self"])
    for k in ("self_state", "practice", "feedback", "incident"):
        assert k in section


async def test_self_state_single_active_per_facet(db):
    await _seed_self(db)
    await write_claim(db, await _claim("ss-1", "self_state", "primary model: glm-5.3-flash"))
    await write_claim(db, await _claim("ss-2", "self_state", "primary model: gpt-5.4"))
    await write_claim(db, await _claim("ss-3", "self_state", "voice: stock joyous"))

    rows = {r["id"]: r for r in await db.fetch_all(
        "SELECT id, status FROM memory_claims WHERE subject_id = 'self-bob'")}
    assert rows["ss-1"]["status"] == "superseded", "same facet replaced"
    assert rows["ss-2"]["status"] == "active"
    assert rows["ss-3"]["status"] == "active", "different facet coexists"


# ---------------------------------------------------------------------------
# self-brief
# ---------------------------------------------------------------------------

async def _seed_brief_claims(db) -> None:
    await _seed_self(db)
    async def put(cid, t, v):
        await write_claim(db, await _claim(cid, t, v))
    await put("b-1", "self_state", "primary model: glm-5.3-flash")
    await put("b-2", "self_image", "A careful, slightly anxious agent.")
    await put("b-3", "practice", "Query the source system before answering schedule questions.")
    await put("b-4", "limit", "Cannot self-modify. Ever. At all.")
    # Excluded types — must never render:
    await put("b-5", "capability", "CAPABILITY-NOISE-MARKER full build story here")
    await put("b-6", "milestone", "MILESTONE-NOISE-MARKER first something")
    await put("b-7", "feedback", "FEEDBACK-NOISE-MARKER someone said something")
    await put("b-8", "incident", "INCIDENT-NOISE-MARKER one time in band camp")


async def test_self_brief_whitelist_and_sections(db):
    from server.services.memory.self_brief import render_self_brief
    await _seed_brief_claims(db)

    brief = await render_self_brief(db)
    assert "Self-Brief" in brief
    assert "primary model: glm-5.3-flash" in brief
    assert "careful, slightly anxious" in brief
    assert "Query the source system" in brief
    assert "Cannot self-modify" in brief
    for marker in ("CAPABILITY-NOISE-MARKER", "MILESTONE-NOISE-MARKER",
                   "FEEDBACK-NOISE-MARKER", "INCIDENT-NOISE-MARKER"):
        assert marker not in brief, "excluded type leaked into the brief"


async def test_self_brief_truncates_to_first_sentence(db):
    from server.services.memory.self_brief import render_self_brief
    await _seed_self(db)
    story = ("Embargo bypasses are a recurring failure mode — three breaches on "
             "2026-08-30 alone: two Aussie BBQ Hour leaks, then the Beatles 101 "
             "build fetching 7 tracks straight into the live library, which is "
             "exactly the class of thing quarantine exists to prevent.")
    await write_claim(db, await _claim("t-1", "limit", story))

    brief = await render_self_brief(db)
    assert "Embargo bypasses are a recurring failure mode" in brief
    assert "Beatles 101" not in brief, "story tail must be truncated away"
    assert len(brief) < 1200


async def test_self_brief_row_cap_and_overflow_line(db):
    from server.services.memory.self_brief import render_self_brief
    await _seed_self(db)
    for i in range(20):
        await write_claim(db, await _claim(f"c-{i}", "limit", f"Limit number {i}."))
    brief = await render_self_brief(db)
    shown = [l for l in brief.splitlines() if l.startswith("- Limit number")]
    assert len(shown) == 15, "cap is 15 rows"
    assert "more — use recall" in brief    # overflow hint


async def test_self_brief_kill_switch(db, monkeypatch):
    from server.services.memory.self_brief import self_brief_block
    await _seed_brief_claims(db)
    monkeypatch.setenv("BOB_SELF_BRIEF", "off")
    assert await self_brief_block(db) == ""
    monkeypatch.setenv("BOB_SELF_BRIEF", "1")
    assert "Self-Brief" in await self_brief_block(db)


async def test_self_brief_cache_invalidates_on_change(db):
    from server.services.memory.self_brief import render_self_brief
    await _seed_self(db)
    await write_claim(db, await _claim("k-1", "practice", "First rule."))
    first = await render_self_brief(db)
    assert "First rule." in first

    await write_claim(db, await _claim("k-2", "practice", "Second rule."))
    second = await render_self_brief(db)
    assert "Second rule." in second, "new claim must invalidate the cache"
    assert "First rule." in second


async def test_workspace_prompt_appends_brief(db, tmp_path, monkeypatch):
    """The system prompt carries the brief as a tail block — the mtime-cached
    base stays stable, the suffix rides along."""
    from server.services.prompt_assembler import load_workspace_prompt
    monkeypatch.setenv("BOB_SELF_BRIEF", "1")
    await _seed_brief_claims(db)

    prompt = await load_workspace_prompt(tmp_path, db)
    assert "Self-Brief" in prompt
    assert "primary model: glm-5.3-flash" in prompt
    assert "CAPABILITY-NOISE-MARKER" not in prompt
