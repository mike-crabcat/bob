"""Claim router tests (bob-events-plan.md Phase 2, §2.4).

The motivating scenario — attendance stated in a group chat updates a
DM-born lunch goal — plus the wrong-slug case, watermark replay durability,
echo suppression, probe fail-open, and the entity-identity mechanisms.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from bob_server.repositories.goals import GoalRepository
from bob_server.services import goal_service

GROUP_KEY = "agent:main:whatsapp:group:doom"
DM_KEY = "agent:main:whatsapp:dm:61400000001"


def _reviser_json(state: dict, *, wake_needed: bool = False,
                  summary: str = "") -> str:
    return json.dumps({"state": {"v": 2, **state}, "wake_needed": wake_needed,
                       "wake_summary": summary})


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    return wake


@pytest.fixture
def llm_chat(monkeypatch):
    """Mock every cheap-model chat (reviser + probe); set .side_effect per test."""
    from bob_server.services.llm_dispatch import LLMDispatchService
    mock = AsyncMock(return_value=_reviser_json({"plan": "waiting"}))
    monkeypatch.setattr(LLMDispatchService, "chat", mock)
    return mock


async def _seed_entity(db, entity_id: str, entity_type: str = "event",
                       display_name: str | None = None) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO memory_entities "
        "(entity_id, entity_type, display_name, status, created_at) "
        "VALUES (?, ?, ?, 'active', datetime('now'))",
        (entity_id, entity_type, display_name or entity_id))


async def _seed_claim(db, claim_id: str, subject: str, claim_type: str,
                      *, value: str | None = None, object_id: str | None = None,
                      message_ids: list[str]) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO memory_claims "
        "(id, claim_type_key, subject_id, object_id, value, status, "
        " source_messages, visibility, scope, created_at, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, 'channel', '[]', datetime('now'), '[]')",
        (claim_id, claim_type, subject, object_id, value,
         json.dumps(message_ids)),
    )


async def _seed_message(db, message_id: str, conversation_id: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, 'user', 'msg', datetime('now'))",
        (message_id, conversation_id),
    )


async def _seed_participant(db, conversation_id: str, contact_id: str) -> None:
    kind = "group" if ":group:" in conversation_id else "dm"
    await db.execute(
        "INSERT OR IGNORE INTO conversations (id, kind, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        (conversation_id, kind),
    )
    await db.execute(
        "INSERT OR IGNORE INTO contacts "
        "(id, name, phone_number, created_at, updated_at) "
        "VALUES (?, 'Mike', ?, datetime('now'), datetime('now'))",
        (contact_id, contact_id),
    )
    await db.execute(
        "INSERT OR REPLACE INTO participants "
        "(conversation_id, identifier, display_name, contact_id, is_trusted, last_active_at) "
        "VALUES (?, ?, ?, ?, 1, datetime('now'))",
        (conversation_id, contact_id, contact_id, contact_id),
    )


async def _seed_goal_with_entity_ref(ctx, entity_id: str) -> str:
    goal = await goal_service.create_goal(
        ctx, conversation_id=DM_KEY, objective="plan the team lunch",
        origin_conversation_id="asker",
        strategy={"v": 2, "plan": "negotiate", "known": [],
                  "open_questions": [], "next_actions": [],
                  "refs": {"entities": [entity_id], "claims": []}})
    return goal["id"]


async def _run_batch(ctx, *, marker: str) -> dict:
    from bob_server.services.memory.claim_router import handle_extraction_batch
    return await handle_extraction_batch(ctx, session_key=GROUP_KEY,
                                         turn_message_id=marker)


# ---------------------------------------------------------------------------
# §2.1 mentions index
# ---------------------------------------------------------------------------

async def test_write_claim_updates_mentions(ctx, db):
    from bob_server.services.memory.claim_service import write_claim
    from bob_server.services.memory.models import Claim
    from datetime import datetime

    await _seed_entity(db, "person-alice", "person")
    await _seed_message(db, "msg-1", "conv-group")
    claim = Claim(id="claim-t1", claim_type_key="name", subject_id="person-alice",
                  value="Alice", status="active",
                  source_messages=["msg-1"], created_at=datetime.now())
    await write_claim(db, claim)

    row = await db.fetch_one(
        "SELECT * FROM memory_entity_mentions WHERE entity_id = 'person-alice'")
    assert row is not None and row["conversation_id"] == "conv-group"
    assert row["first_message_id"] == "msg-1"


async def test_refresh_mentions_after_marker_exists(ctx, db):
    """Extraction writes claims referencing a marker message that doesn't
    exist yet; the post-turn refresh closes the gap."""
    from bob_server.services.memory.claim_router import refresh_mentions_for_turn

    await _seed_entity(db, "event-team-lunch")
    # Claim written before the marker message exists (extraction order).
    await _seed_claim(db, "claim-t2", "event-team-lunch", "start_time",
                      value="2026-09-01T13:00", message_ids=["msg-extr-x1"])
    assert await db.fetch_one(
        "SELECT * FROM memory_entity_mentions WHERE entity_id = 'event-team-lunch'") is None

    await _seed_message(db, "msg-extr-x1", "conv-group")
    await refresh_mentions_for_turn(db, "msg-extr-x1")
    row = await db.fetch_one(
        "SELECT * FROM memory_entity_mentions WHERE entity_id = 'event-team-lunch'")
    assert row is not None and row["conversation_id"] == "conv-group"


# ---------------------------------------------------------------------------
# §2.3 routing — the motivating scenario
# ---------------------------------------------------------------------------

async def test_group_chat_claim_updates_dm_born_goal(ctx, db, mock_wake, llm_chat):
    """Attendance said in the GROUP chat reaches the DM-born lunch goal via
    refs match; silent when the answer was already known."""
    goal_id = await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_entity(db, "person-alice", "person")
    await _seed_message(db, "msg-extr-m1", GROUP_KEY)
    await _seed_claim(db, "claim-r1", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-m1"])

    llm_chat.return_value = _reviser_json(
        {"plan": "negotiate", "known": ["alice attending"],
         "open_questions": [], "next_actions": [],
         "refs": {"entities": ["event-team-lunch"], "claims": []}})

    result = await _run_batch(ctx, marker="msg-extr-m1")
    assert result["status"] == "routed" and result["delivered"] == 1

    goal = await GoalRepository(db).get(goal_id)
    assert "alice attending" in goal["strategy_json"]
    mock_wake.assert_not_awaited(), "silent when next_actions unchanged"

    log = await db.fetch_one("SELECT * FROM memory_routing_log")
    assert log is not None and log["match_type"] == "ref"
    assert log["probe_verdict"] == "skipped", "ref matches skip the probe"


async def test_wake_when_next_actions_change(ctx, db, mock_wake, llm_chat):
    goal_id = await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_message(db, "msg-extr-m2", GROUP_KEY)
    await _seed_claim(db, "claim-r2", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-m2"])
    await _seed_entity(db, "person-alice", "person")

    llm_chat.return_value = _reviser_json(
        {"plan": "quorum reached"}, wake_needed=True, summary="quorum reached")

    result = await _run_batch(ctx, marker="msg-extr-m2")
    assert result["delivered"] == 1
    mock_wake.assert_awaited_once()
    assert mock_wake.await_args.args[1] == DM_KEY
    assert mock_wake.await_args.kwargs.get("call_category") == "goal_progress"


# ---------------------------------------------------------------------------
# §2.0 identity — wrong slug + layers 1/2
# ---------------------------------------------------------------------------

async def test_wrong_slug_routes_via_participant_overlap(ctx, db, mock_wake, llm_chat):
    """The extractor minted `event-lunch` in the group chat (wrong slug, no
    ref match, no mentions). Human participant overlap (mike is in both the
    group and the DM holding the goal) + a RELEVANT probe still deliver it."""
    goal_id = await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    # No event-team-lunch mention anywhere; a fresh wrong-slug entity instead.
    await _seed_entity(db, "event-lunch")
    await _seed_message(db, "msg-extr-m3", GROUP_KEY)
    await _seed_claim(db, "claim-r3", "event-lunch", "start_time",
                      value="2026-09-01T13:00", message_ids=["msg-extr-m3"])

    # mike participates in both conversations (contact-based; the agent has
    # no contact row, so it cannot over-match).
    await _seed_participant(db, GROUP_KEY, "contact-mike")
    await _seed_participant(db, DM_KEY, "contact-mike")

    llm_chat.side_effect = None
    llm_chat.side_effect = [
        json.dumps({"verdict": "RELEVANT"}),          # probe
        _reviser_json({"plan": "consolidate with team-lunch"}),
    ]

    result = await _run_batch(ctx, marker="msg-extr-m3")
    assert result["delivered"] == 1
    goal = await GoalRepository(db).get(goal_id)
    assert "consolidate" in goal["strategy_json"]

    log = await db.fetch_one(
        "SELECT * FROM memory_routing_log WHERE match_type = 'participant'")
    assert log is not None and log["probe_verdict"] == "relevant"


async def test_probe_ignore_suppresses_weak_match(ctx, db, mock_wake, llm_chat):
    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-lunch")
    await _seed_message(db, "msg-extr-m4", GROUP_KEY)
    await _seed_claim(db, "claim-r4", "event-lunch", "start_time",
                      value="x", message_ids=["msg-extr-m4"])
    await _seed_participant(db, GROUP_KEY, "contact-mike")
    await _seed_participant(db, DM_KEY, "contact-mike")

    llm_chat.side_effect = [
        json.dumps({"verdict": "IGNORE"}),
        _reviser_json({"plan": "should not run"}),
    ]
    result = await _run_batch(ctx, marker="msg-extr-m4")
    assert result["delivered"] == 0 and result["probe_ignored"] == 1
    assert llm_chat.await_count == 1, "reviser never runs for an ignored match"


async def test_probe_failure_fails_open(ctx, db, mock_wake, llm_chat):
    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-lunch")
    await _seed_message(db, "msg-extr-m5", GROUP_KEY)
    await _seed_claim(db, "claim-r5", "event-lunch", "start_time",
                      value="x", message_ids=["msg-extr-m5"])
    await _seed_participant(db, GROUP_KEY, "contact-mike")
    await _seed_participant(db, DM_KEY, "contact-mike")

    llm_chat.side_effect = [
        RuntimeError("probe down"),
        _reviser_json({"plan": "delivered via fail-open"}),
    ]
    result = await _run_batch(ctx, marker="msg-extr-m5")
    assert result["delivered"] == 1, "probe errors deliver (fail open)"


async def test_candidate_entities_block_seeds_extractor(ctx, db):
    from bob_server.services.memory.claim_router import build_candidate_entities_block

    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch", display_name="Team Lunch")

    block = await build_candidate_entities_block(db, DM_KEY)
    assert "event-team-lunch" in block
    assert "Team Lunch" in block
    assert "REUSE" in block

    assert await build_candidate_entities_block(db, "agent:main:whatsapp:dm:999") == ""


async def test_create_entity_soft_resolution_steers_reuse(ctx, db):
    from bob_server.services.memory.extraction_tools import make_extraction_tools

    await _seed_entity(db, "event-team-lunch", display_name="Lunch")
    tools = {t.name: t for t in make_extraction_tools(db, "msg-extr-x")}
    out = await tools["create_entity"].handler(
        entity_id="event-lunch", entity_type="event")
    assert "event-team-lunch" in out and "reuse" in out.lower()
    assert await db.fetch_one(
        "SELECT 1 FROM memory_entities WHERE entity_id = 'event-lunch'") is None


# ---------------------------------------------------------------------------
# §2.2 durability — echo suppression, kill switch, watermark replay
# ---------------------------------------------------------------------------

async def test_echo_suppression_holder_only_match(ctx, db, mock_wake, llm_chat):
    """A goal held ONLY by the originating conversation is not a candidate
    through that holder link."""
    goal = await goal_service.create_goal(
        ctx, conversation_id=GROUP_KEY, objective="group-only goal",
        strategy={"v": 2, "plan": "x", "refs": {"entities": []}})
    await _seed_entity(db, "event-solo")
    await _seed_message(db, "msg-extr-m6", GROUP_KEY)
    await _seed_claim(db, "claim-r6", "event-solo", "name",
                      value="solo", message_ids=["msg-extr-m6"])
    # Mention exists in the group conversation (holder of the goal).
    await db.execute(
        "INSERT OR IGNORE INTO memory_entity_mentions VALUES "
        "('event-solo', ?, 'msg-g1', 'msg-extr-m6', datetime('now'), datetime('now'))",
        (GROUP_KEY,))
    del goal  # created only to register the holder

    result = await _run_batch(ctx, marker="msg-extr-m6")
    assert result["candidates"] == 0
    mock_wake.assert_not_awaited()


async def test_kill_switch_holds_watermark_then_replays(ctx, db, mock_wake,
                                                        llm_chat, monkeypatch):
    from bob_server.services.memory import claim_router as router

    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_message(db, "msg-extr-m7", GROUP_KEY)
    await _seed_claim(db, "claim-r7", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-m7"])
    await _seed_entity(db, "person-alice", "person")

    monkeypatch.setenv("BOB_CLAIM_ROUTER_DISABLED", "1")
    result = await _run_batch(ctx, marker="msg-extr-m7")
    assert result["status"] == "disabled"
    # The event is durable; the watermark did not move.
    ev = await db.fetch_one(
        "SELECT * FROM event_log WHERE event_type = 'memory.claims_created'")
    assert ev is not None
    assert await router.get_watermark(db) is None
    mock_wake.assert_not_awaited()

    monkeypatch.delenv("BOB_CLAIM_ROUTER_DISABLED")
    llm_chat.return_value = _reviser_json({"plan": "replayed"})
    replayed = await router.replay_pending(ctx)
    assert replayed == 1
    wm = await router.get_watermark(db)
    assert wm is not None and wm >= ev["id"]
    assert await router.replay_pending(ctx) == 0, "watermark advanced past event"


async def test_replay_after_inline_delivery_is_idempotent(ctx, db, mock_wake,
                                                          llm_chat):
    from bob_server.services.memory import claim_router as router

    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_message(db, "msg-extr-m8", GROUP_KEY)
    await _seed_claim(db, "claim-r8", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-m8"])
    await _seed_entity(db, "person-alice", "person")
    llm_chat.return_value = _reviser_json({"plan": "inline"})

    await _run_batch(ctx, marker="msg-extr-m8")
    effects_before = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM effects WHERE kind = 'goal_revise_state'")

    await router.advance_watermark(db, "")  # rewind: force a replay
    await router.replay_pending(ctx)
    effects_after = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM effects WHERE kind = 'goal_revise_state'")
    assert effects_before["n"] == effects_after["n"] == 1


async def test_routed_event_is_awaited_on_the_bus(ctx, db, mock_wake, llm_chat):
    """The telemetry publish is a coroutine and must be awaited — it was a
    bare call until 2026-08-31, so every routing sweep logged 'coroutine
    EventBus.publish was never awaited' and no event ever reached the bus."""
    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_entity(db, "person-alice", "person")
    await _seed_message(db, "msg-extr-m9", GROUP_KEY)
    await _seed_claim(db, "claim-r9", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-m9"])

    published: list[tuple[str, dict]] = []

    class _Bus:
        async def publish(self, event_type, payload):
            published.append((event_type, payload))

    ctx.event_bus = _Bus()
    await _run_batch(ctx, marker="msg-extr-m9")
    assert [(t, p["claims"]) for t, p in published] == \
        [("memory.claims_created", 1)]
