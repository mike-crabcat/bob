"""Probe engagement policy + STAND_DOWN reaction tier (2026-09-03).

Bob was replying to group chatter he wasn't part of; the probe's ambiguous
default leaned ACT. Pinned here:
- the prompt carries an engagement rule with STAND_DOWN as the ambiguous
  default, and an Engagement state line computed from real history
- the reaction tier: clips are offered in the prompt only when enabled,
  parsed only on STAND_DOWN, validated against the registry
- the coordinator decorates silence: clip sent on STAND_DOWN+react via the
  effects outbox and recorded to history; cooldown, kill switch, missing
  file, and ACT all suppress the send — and no failure path breaks the gate
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from server.services.attention import coordinator as coord_mod
from server.services.attention.coordinator import AttentionCoordinator
from server.services.attention.tier2 import (
    REACTION_CLIPS,
    probe_decide,
    probe_system_prompt,
    reactions_enabled,
)
from server.repositories.conversations import ConversationRepository
from server.services.session_service import SessionService

SESSION = "agent:main:whatsapp:group:react"

GROUP_WINDOW_DRAIN = 0.25  # > fast group window, enough for close+send


@pytest.fixture(autouse=True)
def fast_windows(monkeypatch):
    monkeypatch.setattr(coord_mod, "WINDOW_ADDRESSED_S", 0.02)
    monkeypatch.setattr(coord_mod, "WINDOW_GROUP_S", 0.06)
    monkeypatch.setattr(coord_mod, "TYPING_EXTEND_S", 0.05)
    monkeypatch.setattr(coord_mod, "MAX_WAIT_S", 0.5)
    monkeypatch.setattr(coord_mod, "WAIT_EXTEND_S", 0.03)
    AttentionCoordinator.reset_all()
    yield
    AttentionCoordinator.reset_all()


@pytest.fixture(autouse=True)
def reactions_on(monkeypatch):
    monkeypatch.delenv("BOB_PROBE_REACTIONS", raising=False)
    assert reactions_enabled()
    yield


# -- prompt -----------------------------------------------------------------

def test_prompt_carries_engagement_rule_and_stand_down_default():
    p = probe_system_prompt("Bob", reactions=False)
    assert "Engagement rule (the core policy)" in p
    assert "When ambiguous: STAND_DOWN" in p
    # engagement state is trusted over the model's own transcript read
    assert "Engagement state line is computed from the real history" in p


def test_prompt_offers_clips_when_enabled():
    p = probe_system_prompt("Bob", reactions=True)
    for clip in ("bob-celebrate", "bob-popcorn-cinema", "bob-typing-desk-fire"):
        assert clip in p
    # the high bar is explicit
    assert "ONLY when the moment genuinely hits" in p
    assert "most STAND_DOWNs send no clip" in p
    assert '"react"' in p


def test_prompt_hides_clips_when_disabled(monkeypatch):
    monkeypatch.setenv("BOB_PROBE_REACTIONS", "off")
    assert not reactions_enabled()
    p = probe_system_prompt("Bob", reactions=reactions_enabled())
    assert "bob-celebrate" not in p
    assert '"react"' not in p


# -- parsing ----------------------------------------------------------------

async def _probe_returning(ctx, reply: str):
    async def _chat(self, messages, **k):
        return reply
    with patch("server.services.llm_dispatch.LLMDispatchService.chat", new=_chat):
        return await probe_decide(ctx, "context", session_key=SESSION)


async def test_react_parsed_on_stand_down(ctx):
    r = await _probe_returning(ctx, json.dumps(
        {"decision": "STAND_DOWN", "react": "bob-popcorn-cinema",
         "reason": "drama"}))
    assert r["decision"] == "STAND_DOWN"
    assert r["react"] == "bob-popcorn-cinema"


async def test_react_dropped_for_unknown_clip(ctx):
    r = await _probe_returning(ctx, json.dumps(
        {"decision": "STAND_DOWN", "react": "bob-dab", "reason": "?"}))
    assert r["react"] is None


async def test_react_never_rides_act(ctx):
    r = await _probe_returning(ctx, json.dumps(
        {"decision": "ACT", "react": "bob-celebrate", "reason": "answering"}))
    assert r["decision"] == "ACT"
    assert r["react"] is None


async def test_react_dropped_when_tier_disabled(ctx, monkeypatch):
    monkeypatch.setenv("BOB_PROBE_REACTIONS", "off")
    r = await _probe_returning(ctx, json.dumps(
        {"decision": "STAND_DOWN", "react": "bob-celebrate", "reason": "win"}))
    assert r["react"] is None


async def test_raw_text_still_parses_without_react(ctx):
    r = await _probe_returning(ctx, "STAND_DOWN — banter")
    assert r["decision"] == "STAND_DOWN"
    assert r["react"] is None


async def test_unparseable_defaults_act(ctx):
    r = await _probe_returning(ctx, "garbage ++")
    assert r["decision"] == "ACT"
    assert r["react"] is None


# -- engagement state in the context ----------------------------------------

async def test_engagement_state_computed_from_history(ctx, db):
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(db).ensure(SESSION)
    svc = SessionService(ctx)
    await svc.add_message(SESSION, "user", "anyone tried it?", channel="whatsapp")
    await svc.add_message(SESSION, "assistant", "Yes — solid.", channel="whatsapp")
    await svc.add_message(SESSION, "user", "nice one", channel="whatsapp")
    await svc.add_message(SESSION, "user", "lol", channel="whatsapp", dispatched=0)

    from server.services.attention.tier2 import _build_context
    text = await _build_context(ctx, SESSION, 10)
    state = text.split("## Engagement state")[1].split("##")[0]
    # Engagement counts the DISPATCHED transcript (dispatched_only=True);
    # the pending batch rides separately below it. Dispatched rows here:
    # user, assistant(Bob), user → Bob is 1 message back.
    assert "Bob last spoke 1 message(s) ago" in state
    assert "minutes ago" in state


async def test_engagement_state_absent_when_bot_never_spoke(ctx, db):
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(db).ensure(SESSION)
    svc = SessionService(ctx)
    await svc.add_message(SESSION, "user", "banter", channel="whatsapp")
    await svc.add_message(SESSION, "assistant", "NO_REPLY", channel="whatsapp")

    from server.services.attention.tier2 import _build_context
    text = await _build_context(ctx, SESSION, 10)
    state = text.split("## Engagement state")[1].split("##")[0]
    # NO_REPLY is bookkeeping, not speaking — the bot counts as not engaged.
    assert "has not spoken" in state


# -- coordinator reaction delivery ------------------------------------------

def _clip_in_workspace(tmp_path, name="bob-popcorn-cinema"):
    clip = tmp_path / "self" / "bob" / "avatar" / "reactions" / f"{name}.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00mp4")
    return clip


async def _stand_down_with_react(ctx, tmp_path, monkeypatch, *,
                                react="bob-popcorn-cinema",
                                seed_cooldown=False):
    _clip_in_workspace(tmp_path)
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)

    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(ctx.db).ensure(SESSION)
    if seed_cooldown:
        await SessionService(ctx).add_message(
            SESSION, "assistant", f"[reaction] {react}.mp4",
            channel="whatsapp", provenance="probe_reaction")

    sent: dict = {}

    async def _prepare(path):
        sent["prepared"] = path
        return path

    async def _emit(ctx_, **kw):
        sent["effect"] = kw
        return {"ok": True, "effect_id": "e1"}

    with patch("server.services.attention.tier2.probe_actionability",
               new=AsyncMock(return_value={"decision": "STAND_DOWN",
                                           "react": react, "reason": "hit"})), \
         patch("server.services.whatsapp_bridge_service._media._prepare_media",
               new=_prepare), \
         patch("server.services.effects.emit_and_deliver", new=_emit):
        dispatch = AsyncMock()
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="WE WON THE FINAL!", chat_kind="group",
            probe_enabled=True)
        await asyncio.sleep(GROUP_WINDOW_DRAIN)
    return sent, dispatch


async def test_stand_down_with_react_sends_clip(ctx, db, tmp_path, monkeypatch):
    sent, dispatch = await _stand_down_with_react(ctx, tmp_path, monkeypatch)
    dispatch.assert_not_awaited()  # silence kept: no main-LLM turn
    eff = sent.get("effect")
    assert eff and eff["kind"] == "whatsapp_send_media"
    assert eff["payload"]["chat_id"].endswith("@g.us")
    assert "bob-popcorn-cinema.mp4" in eff["payload"]["file_path"]
    cid = await ConversationRepository(db).resolve_cid(SESSION)
    row = await db.fetch_one(
        "SELECT content FROM messages WHERE conversation_id = ? "
        "AND role = 'assistant' ORDER BY rowid DESC LIMIT 1", (cid,))
    assert row and row["content"] == "[reaction] bob-popcorn-cinema.mp4"


async def test_reaction_cooldown_suppresses_second_clip(ctx, db, tmp_path, monkeypatch):
    sent, dispatch = await _stand_down_with_react(
        ctx, tmp_path, monkeypatch, seed_cooldown=True)
    dispatch.assert_not_awaited()
    assert "effect" not in sent, "cooldown must suppress the send"


async def test_reaction_kill_switch(ctx, db, tmp_path, monkeypatch):
    monkeypatch.setenv("BOB_PROBE_REACTIONS", "off")
    sent, dispatch = await _stand_down_with_react(ctx, tmp_path, monkeypatch)
    dispatch.assert_not_awaited()
    assert "effect" not in sent


async def test_act_with_react_still_dispatches_without_clip(
        ctx, db, tmp_path, monkeypatch):
    _clip_in_workspace(tmp_path)
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(ctx.db).ensure(SESSION)

    sent: dict = {}

    async def _emit(ctx_, **kw):
        sent["effect"] = kw
        return {"ok": True}

    with patch("server.services.attention.tier2.probe_actionability",
               new=AsyncMock(return_value={"decision": "ACT",
                                           "react": "bob-celebrate",
                                           "reason": "answering"})), \
         patch("server.services.effects.emit_and_deliver", new=_emit):
        dispatch = AsyncMock()
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="what time is it?", chat_kind="group",
            probe_enabled=True)
        await asyncio.sleep(GROUP_WINDOW_DRAIN)
    dispatch.assert_awaited_once()      # a real answer, not a clip
    assert "effect" not in sent


async def test_missing_clip_file_never_breaks_the_gate(ctx, db, tmp_path, monkeypatch):
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)  # no clip file
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(ctx.db).ensure(SESSION)
    await SessionService(ctx).add_message(
        SESSION, "user", "banter", channel="whatsapp", dispatched=0)

    with patch("server.services.attention.tier2.probe_actionability",
               new=AsyncMock(return_value={"decision": "STAND_DOWN",
                                           "react": "bob-celebrate",
                                           "reason": "win"})):
        dispatch = AsyncMock()
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="we won!", chat_kind="group",
            probe_enabled=True)
        await asyncio.sleep(GROUP_WINDOW_DRAIN)
    dispatch.assert_not_awaited()
    row = await db.fetch_one(
        "SELECT dispatched FROM messages WHERE conversation_id IS NOT NULL "
        "AND role = 'user' ORDER BY rowid DESC LIMIT 1")
    assert row and row["dispatched"] == 1, "messages still flushed on stand-down"


def test_registry_matches_manifest_names():
    assert set(REACTION_CLIPS) == {
        "bob-celebrate", "bob-patience-v2", "bob-this-is-fine",
        "bob-popcorn-cinema", "bob-awkward-standing", "bob-typing-desk-fire",
        "bob-typing-desk-fire-closeup",
    }
