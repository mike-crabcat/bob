"""Curated self-model (services/dream/self_model.py — prompt layout plan
Phase 2b)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from server.services.dream import self_model as sm
from server.services.memory.claim_service import write_claim
from server.services.memory.models import Claim


async def _seed(db, cid, ctype, value, created=None):
    await write_claim(db, Claim(id=cid, claim_type_key=ctype, subject_id="self-bob",
                                value=value, created_at=created or datetime.now()))


CURATED = {
    "state": ["Groups run on gpt-6-luna; the attention probe on Luna Decisions."],
    "practices": ["Verify today's date before date-stamped posts.",
                  "Judge photos on what's in them, not the banter."],
    "limits": ["Video understanding is sampled frames only."],
    "watchouts": ["Beer-judging: show the evidence before a verdict."],
    "retire": [{"id": "c-stale", "reason": "stale: list_groups exists"},
               {"id": "not-a-claim", "reason": "stale: x y z"},
               {"id": "c-keep", "reason": "Merged into practice: verify dates"}],
    "move_to_skill": [{"id": "c-howto", "skill": "openai-image", "note": "flags"}],
    "social": ["Leeming: judging calls get mocked — show photo evidence first."],
    "tone_today": "dry, a little smug",
}


async def _seeded(ctx):
    from server.services.memory import MemoryService
    await MemoryService(ctx).ensure_self_entity()
    await _seed(ctx.db, "c-stale", "limit", "No tool exists to list all WhatsApp conversations.")
    await _seed(ctx.db, "c-howto", "practice", "openai_image.py has no --reference flag.")
    await _seed(ctx.db, "c-keep", "practice", "Verify today's date before date-stamped posts.")


def test_validate_clamps_and_drops_unknown_ids():
    raw = {**CURATED, "practices": [f"p{i}" for i in range(30)]}
    out = sm.validate(raw, {"c-stale", "c-howto"}, social=True)
    assert len(out["practices"]) == sm.CAPS["practices"]
    assert [r["id"] for r in out["retire"]] == ["c-stale"]
    assert out["social"] and out["tone_today"]
    with pytest.raises(ValueError):
        sm.validate({"state": [], "practices": []}, set(), social=False)
    assert "social" not in sm.validate(CURATED, set(), social=False)


def test_render_and_social_fence():
    text = sm.render(sm.validate(CURATED, set(), social=True))
    assert "How I work:" in text and "Known limits:" in text
    social = sm.render_social(sm.validate(CURATED, set(), social=True))
    assert "never effort, helpfulness or accuracy" in social
    assert "Leeming" in social


async def test_curate_stores_retires_and_digests(ctx):
    await _seeded(ctx)
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value=json.dumps(CURATED))), \
         patch.object(sm, "_social_signals", new=AsyncMock(return_value="[Leeming] ...")):
        row = await sm.curate(ctx)
    assert row["status"] == "ready"
    assert "retired 1 stale" in row["digest"]
    status = await ctx.db.fetch_one(
        "SELECT status, superseded_by FROM memory_claims WHERE id = 'c-stale'")
    assert status["status"] == "superseded" and "dream_curation" in status["superseded_by"]
    keep = await ctx.db.fetch_one("SELECT status FROM memory_claims WHERE id = 'c-howto'")
    assert keep["status"] == "active"  # how-tos are proposed, never auto-moved
    assert (await sm.latest_ready(ctx.db))["id"] == row["id"]


async def test_dry_run_writes_nothing(ctx):
    await _seeded(ctx)
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value=json.dumps(CURATED))), \
         patch.object(sm, "_social_signals", new=AsyncMock(return_value="")):
        row = await sm.curate(ctx, apply=False)
    assert row["status"] == "ready"
    assert await sm.latest_ready(ctx.db) is None
    st = await ctx.db.fetch_one("SELECT status FROM memory_claims WHERE id = 'c-stale'")
    assert st["status"] == "active"


async def test_bad_llm_output_records_failure(ctx):
    await _seeded(ctx)
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value="not json at all")), \
         patch.object(sm, "_social_signals", new=AsyncMock(return_value="")):
        row = await sm.curate(ctx)
    assert row["status"] == "failed" and row["error"]


async def test_run_if_due_once_per_night(ctx, monkeypatch):
    monkeypatch.setenv("BOB_SELF_CURATION", "on")
    await _seeded(ctx)
    calls = AsyncMock(return_value=json.dumps(CURATED))
    monkeypatch.setattr(sm, "_social_signals", AsyncMock(return_value=""))
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt", new=calls):
        monkeypatch.setattr(sm, "_now", lambda: datetime(2026, 10, 12, 1, 30))
        assert await sm.run_if_due(ctx) is None          # before 02:00
        monkeypatch.setattr(sm, "_now", lambda: datetime(2026, 10, 12, 3, 0))
        assert (await sm.run_if_due(ctx))["status"] == "ready"
        assert await sm.run_if_due(ctx) is None          # once per day
    assert calls.await_count == 1


async def test_curated_brief_delta_and_staleness(ctx, monkeypatch):
    monkeypatch.setenv("BOB_SELF_MODEL_SOCIAL", "off")
    await _seeded(ctx)
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value=json.dumps(CURATED))), \
         patch.object(sm, "_social_signals", new=AsyncMock(return_value="[Leeming] ...")):
        await sm.curate(ctx)
    await _seed(ctx.db, "c-new", "practice", "Post clips via the avatar skill. More detail here.",
                created=datetime.now() + timedelta(seconds=5))
    brief = await sm.curated_brief(ctx.db, now=datetime.now() + timedelta(minutes=1))
    assert "How I work:" in brief
    assert "Since last night" in brief and "avatar skill" in brief
    assert "Social read" not in brief                       # flag off by default
    monkeypatch.setenv("BOB_SELF_MODEL_SOCIAL", "on")
    assert "Social read" in await sm.curated_brief(ctx.db, now=datetime.now())
    assert await sm.curated_brief(ctx.db, now=datetime.now() + timedelta(hours=49)) is None


async def test_self_brief_block_respects_flag(ctx, monkeypatch):
    monkeypatch.delenv("BOB_SELF_MODEL_BRIEF", raising=False)
    from server.services.memory.self_brief import reset_cache, self_brief_block
    await _seeded(ctx)
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value=json.dumps(CURATED))), \
         patch.object(sm, "_social_signals", new=AsyncMock(return_value="")):
        await sm.curate(ctx)
    reset_cache()
    raw = await self_brief_block(ctx.db)
    assert "curated nightly" not in raw                     # default: raw renderer
    monkeypatch.setenv("BOB_SELF_MODEL_BRIEF", "on")
    assert "curated nightly" in await self_brief_block(ctx.db)


async def test_digest_sent_once_after_8(ctx, monkeypatch):
    monkeypatch.setenv("BOB_SELF_CURATION", "on")
    monkeypatch.setenv("BOB_SELF_MODEL_DIGEST", "on")
    await _seeded(ctx)
    monkeypatch.setattr(sm, "_social_signals", AsyncMock(return_value=""))
    monkeypatch.setattr(sm, "_now", lambda: datetime(2026, 10, 12, 3, 0))
    with patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value=json.dumps(CURATED))):
        await sm.run_if_due(ctx)
    send = AsyncMock(return_value={"ok": True})
    with patch("server.services.effects.emit_and_deliver", new=send), \
         patch("server.services.steering.owner_contact",
               new=AsyncMock(return_value={"id": "c1", "phone_number": "+61456224867"})):
        assert await sm.deliver_digest_if_due(ctx) is False   # 03:00 < 08:00
        monkeypatch.setattr(sm, "_now", lambda: datetime(2026, 10, 12, 8, 5))
        assert await sm.deliver_digest_if_due(ctx) is True
        assert await sm.deliver_digest_if_due(ctx) is False   # once
    assert send.await_args.kwargs["payload"]["chat_id"] == "61456224867@s.whatsapp.net"


def test_retire_reasons_are_restricted():
    ids = {"a", "b"}
    assert sm._valid_retire_reason("stale: tool exists now", "a", ids)
    assert sm._valid_retire_reason("duplicate of b", "a", ids)
    assert not sm._valid_retire_reason("duplicate of a", "a", ids)      # itself
    assert not sm._valid_retire_reason("duplicate of zz", "a", ids)     # unknown
    assert not sm._valid_retire_reason("Merged into practice: x", "a", ids)
    assert not sm._valid_retire_reason("stale", "a", ids)               # no reason
