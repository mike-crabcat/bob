"""Silent-turn extraction window + accounting fixes (2026-09-30).

Fix #1: idle extraction reviews only undigested messages (newer than the
previous extraction's ran_at, minus a small overlap). Before this, every
idle turn re-reviewed the whole 30-message tail, so a long-running topic
was re-extracted — and re-announced as "new claims" — at every idle gap
(the verbose-memory duplication report).
Fix #4: claims_created and the verbose notice count only rows created this
turn. A dedup-MERGE writes the turn id into an existing row's
source_messages, which the source_messages LIKE alone miscounted as new.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.services.memory.service import MemoryService
from server.services.session_service import SessionService

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _seed(ctx, key: str, messages: list[tuple[str, str, datetime]]) -> None:
    """Add messages then force their created_at (add_message stamps now)."""
    ss = SessionService(ctx)
    for role, content, at in messages:
        mid = await ss.add_message(key, role, content)
        await ctx.db.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?", (_iso(at), mid)
        )


@pytest.mark.asyncio
async def test_recent_dialogue_since_iso_filters_window(db, ctx):
    from server.repositories.history import HistoryRepository

    key = "agent:main:whatsapp:dm:61000000000"
    await _seed(ctx, key, [
        ("user", "DIGESTED old message", NOW - timedelta(hours=1)),
        ("user", "FRESH new message", NOW - timedelta(minutes=5)),
    ])
    rows = await HistoryRepository(db).recent_dialogue(
        key, limit=30, since_iso=_iso(NOW - timedelta(minutes=55)))
    contents = [r["content"] for r in rows]
    assert "FRESH new message" in contents
    assert "DIGESTED old message" not in contents


@pytest.mark.asyncio
async def test_idle_window_excludes_digested_tail(ctx, monkeypatch):
    """The idle turn's rendered history starts after the previous extraction
    (minus overlap); a still-in-tail topic is not re-fed to the extractor."""
    key = "agent:main:whatsapp:dm:61000000001"
    await _seed(ctx, key, [
        ("user", "DIGESTED irrigation chat", NOW - timedelta(hours=1)),
        ("user", "FRESH figurine update", NOW - timedelta(minutes=10)),
    ])
    # previous extraction ran 50 min ago: boundary = ran_at - 5 min overlap
    ran_at = (NOW - timedelta(minutes=50)).isoformat()
    await ctx.db.execute(
        "INSERT INTO memory_extraction_turns (id, session_key, message_id, ran_at, claims_created) "
        "VALUES ('extr-t1', ?, 'msg-x', ?, 0)", (key, ran_at))

    from server.services.llm_dispatch import LLMDispatchService
    captured: dict[str, list] = {}

    async def fake_chat(self, messages, tools, **kw):
        captured["messages"] = messages
        return "Nothing to record."

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", fake_chat)

    svc = MemoryService(ctx)
    result = await svc.run_silent_turn_extraction(key)
    assert result["status"] == "ok"
    history_text = "\n".join(m["content"] for m in captured["messages"])
    assert "FRESH figurine update" in history_text
    assert "DIGESTED irrigation chat" not in history_text


@pytest.mark.asyncio
async def test_forced_remember_turn_keeps_full_tail(ctx, monkeypatch):
    """force=True (explicit remember / backfill) deliberately re-reads the
    tail — the undigested boundary must not starve it."""
    key = "agent:main:whatsapp:dm:61000000001"
    ran_at = (NOW - timedelta(minutes=50)).isoformat()
    await ctx.db.execute(
        "INSERT INTO memory_extraction_turns (id, session_key, message_id, ran_at, claims_created) "
        "VALUES ('extr-t1', ?, 'msg-x', ?, 0)", (key, ran_at))
    await _seed(ctx, key, [
        ("user", "DIGESTED irrigation chat", NOW - timedelta(hours=1)),
        ("user", "FRESH figurine update", NOW - timedelta(minutes=10)),
    ])

    from server.services.llm_dispatch import LLMDispatchService
    captured: dict[str, list] = {}

    async def fake_chat(self, messages, tools, **kw):
        captured["messages"] = messages
        return "Nothing to record."

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", fake_chat)

    result = await MemoryService(ctx).run_silent_turn_extraction(key, force=True)
    assert result["status"] == "ok"
    history_text = "\n".join(m["content"] for m in captured["messages"])
    assert "DIGESTED irrigation chat" in history_text
    assert "FRESH figurine update" in history_text


@pytest.mark.asyncio
async def test_merged_noop_claim_not_counted_or_announced(ctx, monkeypatch):
    """Re-recording an identical claim merges into the existing row: no new
    row, claims_created == 0, and no verbose notice even with
    memory_verbose on (the LIKE-on-source_messages miscount, fix #4)."""
    key = "agent:main:whatsapp:dm:61000000002"
    await _seed(ctx, key, [
        ("user", "mika prefers evening runs", NOW - timedelta(minutes=5)),
    ])

    # Pre-existing entity + claim, written via the real extraction tools and
    # backdated so the row predates the turn under test.
    from server.services.memory.extraction_tools import make_extraction_tools
    prior = {t.name: t for t in make_extraction_tools(ctx.db, "msg-extr-prior")}
    await prior["create_entity"].handler(
        entity_id="person-test-mika", entity_type="person")
    await prior["add_claim"].handler(
        subject_id="person-test-mika", claim_type_key="interest",
        value="evening runs")
    await ctx.db.execute(
        "UPDATE memory_claims SET created_at = ? WHERE subject_id = 'person-test-mika'",
        (_iso(NOW - timedelta(hours=2)),))

    from server.repositories.conversations import ConversationRepository
    repo = ConversationRepository(ctx.db)
    await repo.ensure(key)
    assert await repo.set_policy(key, {"memory_verbose": True})

    from server.services.llm_dispatch import LLMDispatchService

    async def fake_chat(self, messages, tools, **kw):
        add_claim = {t.name: t for t in tools}["add_claim"]
        await add_claim.handler(
            subject_id="person-test-mika", claim_type_key="interest",
            value="evening runs")  # identical → dedup-merge, no new row
        return "Recorded."

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", fake_chat)

    result = await MemoryService(ctx).run_silent_turn_extraction(key, force=True)
    assert result["status"] == "ok"
    assert result["claims_created"] == 0

    # The merged row's provenance DID gain this turn's id (that's the merge
    # working) — and that is exactly what the old count misread as new.
    row = await ctx.db.fetch_one(
        "SELECT source_messages FROM memory_claims WHERE subject_id = 'person-test-mika'")
    assert result["turn_message_id"] in (row["source_messages"] or "")

    # No verbose notice was posted for this session.
    notice = await ctx.db.fetch_one(
        "SELECT count(*) AS n FROM messages WHERE metadata LIKE '%memory_verbose%'")
    assert notice["n"] == 0
