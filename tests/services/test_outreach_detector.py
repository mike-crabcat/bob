"""Out-of-channel outreach detector tests (2026-08-26 David/coffee incident).

The probe LLM is stubbed at LLMDispatchService.chat. Seeding mirrors the
incident: an outreach goal working David's DM, David's confirmation arriving
in a shared group.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

GROUP = "wa:group:whatsapp:group:1203634"
DAVID_DM = "wa:contact:whatsapp:dm:61401203022"
DAVID_ID = "c-david"

PROBE_SATISFIED = json.dumps({"satisfied": True, "note": "explicit yes to the coffee"})
PROBE_NOT_SATISFIED = json.dumps({"satisfied": False, "note": "unrelated chatter"})


@pytest.fixture(autouse=True)
def clean_executor_registry():
    """The reaper test registers an executor; never leak it into other files."""
    from server.services import effects as effects_svc

    saved = dict(effects_svc._EXECUTORS)
    effects_svc.executors_reset_for_tests()
    yield
    effects_svc._EXECUTORS.clear()
    effects_svc._EXECUTORS.update(saved)


class _ProbeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, messages: list[dict], *, call_category: str, **kwargs: Any) -> str:
        self.calls.append({"category": call_category})
        return self.reply


async def _seed_world(db, *, goal_status: str = "active") -> str:
    """Contacts + conversations + participants + an outreach goal in David's DM."""
    from server.repositories.conversations import ConversationRepository

    conv_repo = ConversationRepository(db)
    group_cid = (await conv_repo.ensure(GROUP))["id"]
    dm_cid = (await conv_repo.ensure(DAVID_DM))["id"]
    await db.execute(
        "INSERT OR IGNORE INTO contacts (id, name, created_at, updated_at) "
        "VALUES (?, 'David Shedden', datetime('now'), datetime('now'))", (DAVID_ID,))
    for cid in (group_cid, dm_cid):
        await db.execute(
            "INSERT OR IGNORE INTO participants (conversation_id, identifier, contact_id, last_active_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (cid, DAVID_ID, DAVID_ID))

    await db.execute(
        """INSERT INTO goals (id, conversation_id, kind, objective, status, created_at, updated_at)
           VALUES ('goal-david', ?, 'outreach',
                   'Get David''s attendance confirmation for the AI Doom coffee', ?,
                   datetime('now'), datetime('now'))""",
        (dm_cid, goal_status))
    return group_cid


async def _append_received(db, *, message_id: str, conversation_id: str,
                           text: str = "Yeah I'm in for the coffee") -> None:
    from server.repositories import Event, EventLogRepository

    await db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, 'user', ?, datetime('now'))",
        (message_id, conversation_id, text))
    await EventLogRepository(db).append(Event(
        event_type="message.received",
        binding_key="test",
        conversation_id=conversation_id,
        source="whatsapp",
        external_id=message_id,
        payload={"session_message_id": message_id, "contact_id": DAVID_ID,
                 "sender_name": "David Shedden", "chat_kind": "group"},
    ))


async def _run_sweep(ctx, monkeypatch, reply: str) -> _ProbeLLM:
    from server.services import outreach_detector

    probe = _ProbeLLM(reply)
    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService.chat", probe.chat)
    await outreach_detector.sweep(ctx)
    return probe


async def test_first_run_positions_watermark_without_probing(ctx, monkeypatch):
    await _seed_world(ctx.db)
    await _append_received(ctx.db, message_id="m1", conversation_id=GROUP)

    probe = await _run_sweep(ctx, monkeypatch, PROBE_SATISFIED)  # first run
    assert probe.calls == []  # history is not back-probed
    # Second run: no new events → still nothing probed.
    await _run_sweep(ctx, monkeypatch, PROBE_SATISFIED)
    assert probe.calls == []


async def test_out_of_channel_confirmation_completes_goal(ctx, monkeypatch):
    from server.services import outreach_detector

    await _seed_world(ctx.db)
    await outreach_detector.sweep(ctx)  # first run: position watermark
    await _append_received(ctx.db, message_id="m2", conversation_id=GROUP)

    probe = await _run_sweep(ctx, monkeypatch, PROBE_SATISFIED)
    assert len(probe.calls) == 1
    assert probe.calls[0]["category"] == "outreach_probe"

    goal = await ctx.db.fetch_one("SELECT status, result FROM goals WHERE id = 'goal-david'")
    assert goal["status"] == "completed"
    assert "David Shedden" in goal["result"]

    row = await ctx.db.fetch_one(
        "SELECT verdict FROM outreach_probe_log WHERE goal_id = 'goal-david' AND message_id = 'm2'")
    assert row["verdict"] == "satisfied"

    # Idempotent: re-running (crash between probe and watermark, say) must
    # not re-probe or re-complete.
    await outreach_detector.sweep(ctx)
    assert len(probe.calls) == 1


async def test_unrelated_group_chatter_leaves_goal_active(ctx, monkeypatch):
    await _seed_world(ctx.db)
    from server.services import outreach_detector
    await outreach_detector.sweep(ctx)
    await _append_received(ctx.db, message_id="m3", conversation_id=GROUP,
                           text="The shirt mockup looks great")

    await _run_sweep(ctx, monkeypatch, PROBE_NOT_SATISFIED)
    goal = await ctx.db.fetch_one("SELECT status FROM goals WHERE id = 'goal-david'")
    assert goal["status"] == "active"
    row = await ctx.db.fetch_one(
        "SELECT verdict FROM outreach_probe_log WHERE message_id = 'm3'")
    assert row["verdict"] == "not_satisfied"


async def test_in_channel_reply_is_not_probed(ctx, monkeypatch):
    """A reply inside the goal's own DM is the DM agent's job — no probe."""
    await _seed_world(ctx.db)
    from server.services import outreach_detector
    await outreach_detector.sweep(ctx)
    await _append_received(ctx.db, message_id="m4", conversation_id=DAVID_DM)

    probe = await _run_sweep(ctx, monkeypatch, PROBE_SATISFIED)
    assert probe.calls == []


async def test_kill_switch_freezes_watermark(ctx, monkeypatch):
    from server.services import outreach_detector

    await _seed_world(ctx.db)
    await outreach_detector.sweep(ctx)
    await _append_received(ctx.db, message_id="m5", conversation_id=GROUP)

    monkeypatch.setenv("BOB_OUTREACH_DETECTOR_DISABLED", "1")
    await _run_sweep(ctx, monkeypatch, PROBE_SATISFIED)

    goal = await ctx.db.fetch_one("SELECT status FROM goals WHERE id = 'goal-david'")
    assert goal["status"] == "active"

    # Lifting the switch replays the gap (claim-router contract).
    monkeypatch.delenv("BOB_OUTREACH_DETECTOR_DISABLED")
    probe = await _run_sweep(ctx, monkeypatch, PROBE_SATISFIED)
    assert len(probe.calls) == 1
    goal = await ctx.db.fetch_one("SELECT status FROM goals WHERE id = 'goal-david'")
    assert goal["status"] == "completed"


async def test_probe_error_fails_closed(ctx, monkeypatch):
    await _seed_world(ctx.db)
    from server.services import outreach_detector
    await outreach_detector.sweep(ctx)
    await _append_received(ctx.db, message_id="m6", conversation_id=GROUP)

    async def _boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("server.services.llm_dispatch.LLMDispatchService.chat", _boom)
    await outreach_detector.sweep(ctx)

    goal = await ctx.db.fetch_one("SELECT status FROM goals WHERE id = 'goal-david'")
    assert goal["status"] == "active"
    row = await ctx.db.fetch_one("SELECT verdict FROM outreach_probe_log WHERE message_id = 'm6'")
    assert row["verdict"] == "error"


# ------------------------------------------------------------------- reaper

async def test_reaper_requeues_stuck_delivering(db):
    from server.repositories.effects import EffectRepository

    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    await db.execute(
        """INSERT INTO effects (id, kind, idempotency_key, payload_json, status,
                                attempt, available_at, created_at, claimed_at)
           VALUES ('eff-stuck', 'goal_revise_state', 'k1', '{}', 'delivering',
                   1, ?, ?, ?)""",
        (old, old, old))
    await db.execute(
        """INSERT INTO effects (id, kind, idempotency_key, payload_json, status,
                                attempt, available_at, created_at, claimed_at)
           VALUES ('eff-fresh', 'goal_revise_state', 'k2', '{}', 'delivering',
                   1, datetime('now'), datetime('now'), datetime('now'))""")

    moved = await EffectRepository(db).requeue_stale_delivering()
    assert moved == 1
    stuck = await db.fetch_one("SELECT status, error FROM effects WHERE id = 'eff-stuck'")
    assert stuck["status"] == "pending"
    assert "stuck delivering" in stuck["error"]
    fresh = await db.fetch_one("SELECT status FROM effects WHERE id = 'eff-fresh'")
    assert fresh["status"] == "delivering"


async def test_reaper_dead_letters_past_max_attempts(db):
    from server.repositories.effects import EffectRepository

    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    await db.execute(
        """INSERT INTO effects (id, kind, idempotency_key, payload_json, status,
                                attempt, available_at, created_at, claimed_at)
           VALUES ('eff-max', 'goal_revise_state', 'k3', '{}', 'delivering',
                   5, ?, ?, ?)""",
        (old, old, old))

    moved = await EffectRepository(db).requeue_stale_delivering()
    assert moved == 1
    row = await db.fetch_one("SELECT status FROM effects WHERE id = 'eff-max'")
    assert row["status"] == "dead"


async def test_pump_requeues_then_claims_stuck_effect(ctx):
    """The stuck eff-227017 class: pump due must rescue it end to end."""
    from server.repositories.effects import EffectRepository
    from server.services.effects import register_executor

    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    await ctx.db.execute(
        """INSERT INTO effects (id, kind, idempotency_key, payload_json, status,
                                attempt, available_at, created_at, claimed_at)
           VALUES ('eff-227017', 'goal_revise_state', 'k4', '{}', 'delivering',
                   1, ?, ?, ?)""",
        (old, old, old))

    delivered: list[str] = []

    async def _exec(c, payload):
        delivered.append(payload.get("goal_id", "?"))
        return "done"

    register_executor("goal_revise_state", _exec, retryable=True)
    from server.services.effects import pump_due_effects
    processed = await pump_due_effects(ctx)
    assert processed == 1
    assert delivered == ["?"]  # payload '{}' — executor ran
    row = await ctx.db.fetch_one("SELECT status FROM effects WHERE id = 'eff-227017'")
    assert row["status"] == "delivered"
