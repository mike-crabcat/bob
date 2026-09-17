"""Fixes for the 2026-09-14 memory-churn diagnosis.

Four regressions surfaced as "recent memory on the dashboard being
re-created continually":

1. The heartbeat reconciliation sweep counted its own claim-recon-* writes
   as touches, so every pass re-enrolled its subjects — a self-feeding loop
   over months-old entities (admin.recently_touched_entity_ids).
2. Reconciliation wrote no-op claims: alias/name equal to the display name,
   and same-value rewrites via supersede (reconciliation tools).
3. Inline claim routing didn't advance the watermark, so the replay sweep
   re-routed every batch — duplicate probe calls + routing-log rows
   (claim_router).
4. The extractor minted near-duplicate entities when naming drifted
   (extraction_tools soft resolution), and journalled dated events onto
   hub entities (silent-turn prompt).
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

GROUP_KEY = "agent:main:whatsapp:group:doom"
DM_KEY = "agent:main:whatsapp:dm:61400000001"


@pytest.fixture(autouse=True)
def _legacy_goal_path(ctx):
    """These tests pin the LEGACY goal machinery (reviser, wake matrix,
    claim-router delivery) — the fallback path under goal rooms. Rooms have
    their own suite: tests/services/test_goal_rooms.py."""
    ctx.settings.goal_rooms.enabled = False
    yield


def _reviser_json(state: dict, *, wake_needed: bool = False,
                  summary: str = "") -> str:
    return json.dumps({"state": {"v": 2, **state}, "wake_needed": wake_needed,
                       "wake_summary": summary})


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("server.services.wake_service.wake_conversation", wake)
    return wake


@pytest.fixture
def llm_chat(monkeypatch):
    """Mock every cheap-model chat (reviser + probe); set .side_effect per test."""
    from server.services.llm_dispatch import LLMDispatchService
    mock = AsyncMock(return_value=_reviser_json({"plan": "waiting"}))
    monkeypatch.setattr(LLMDispatchService, "chat", mock)
    return mock


async def _seed_entity(db, entity_id: str, entity_type: str = "event",
                       display_name: str | None = None, *,
                       age_days: float = 0) -> None:
    """age_days: entity rows created in the last 24h count as a touch on
    their own — pass >1 for entities that should only be touched via claims."""
    await db.execute(
        "INSERT OR IGNORE INTO memory_entities "
        "(entity_id, entity_type, display_name, status, created_at) "
        "VALUES (?, ?, ?, 'active', datetime('now', ?))",
        (entity_id, entity_type, display_name or entity_id,
         f"-{age_days} days"))


async def _seed_claim(db, claim_id: str, subject: str, claim_type: str,
                      *, value: str | None = None, object_id: str | None = None,
                      message_ids: list[str] | None = None) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO memory_claims "
        "(id, claim_type_key, subject_id, object_id, value, status, "
        " source_messages, visibility, scope, created_at, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, 'channel', '[]', datetime('now'), '[]')",
        (claim_id, claim_type, subject, object_id, value,
         json.dumps(message_ids or [])),
    )


async def _seed_message(db, message_id: str, conversation_id: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, 'user', 'msg', datetime('now'))",
        (message_id, conversation_id),
    )


async def _seed_goal_with_entity_ref(ctx, entity_id: str) -> str:
    from server.services import goal_service
    goal = await goal_service.create_goal(
        ctx, conversation_id=DM_KEY, objective="plan the team lunch",
        origin_conversation_id="asker",
        strategy={"v": 2, "plan": "negotiate", "known": [],
                  "open_questions": [], "next_actions": [],
                  "refs": {"entities": [entity_id], "claims": []}})
    return goal["id"]


async def _run_batch(ctx, *, marker: str) -> dict:
    from server.services.memory.claim_router import handle_extraction_batch
    return await handle_extraction_batch(ctx, session_key=GROUP_KEY,
                                         turn_message_id=marker)


# ---------------------------------------------------------------------------
# 1. Reconciliation self-touch loop
# ---------------------------------------------------------------------------

async def test_recon_writes_do_not_reenrol_for_reconciliation(db):
    """A claim-recon-* row must not count as a touch: counting them let the
    sweep feed itself (perpetual re-reconciliation of months-old entities)."""
    from server.services.memory.admin import recently_touched_entity_ids

    await _seed_entity(db, "location-vallorcine", "location", "Vallorcine",
                       age_days=90)
    await _seed_entity(db, "person-mike", "person", "Mike", age_days=90)
    await _seed_entity(db, "event-fresh", "event", "Fresh")

    # Reconciliation's own write on an old entity — must NOT enrol it.
    await _seed_claim(db, "claim-recon-abc1", "location-vallorcine", "alias",
                      value="Vallorcine")
    # Extraction-derived write — must enrol.
    await _seed_claim(db, "claim-extr-abc2", "person-mike", "work_schedule",
                      value="WFH Fridays", message_ids=["msg-extr-1"])
    # Entity created in the last 24h with no claims yet — must enrol.
    # (event-fresh has no claims.)

    ids = await recently_touched_entity_ids(db, limit=10)
    assert "location-vallorcine" not in ids
    assert "person-mike" in ids
    assert "event-fresh" in ids


async def test_superseded_recon_claims_still_do_not_touch(db):
    """Even a superseded-then-rewritten chain leaves no touch once the loop
    fix is in (all rows in the chain carry the claim-recon- prefix)."""
    from server.services.memory.admin import recently_touched_entity_ids

    await _seed_entity(db, "location-flower-clock", "location", "Flower Clock",
                       age_days=90)
    await db.execute(
        "INSERT INTO memory_claims "
        "(id, claim_type_key, subject_id, value, status, source_messages, "
        " visibility, scope, created_at, superseded_by) "
        "VALUES ('claim-recon-old', 'alias', 'location-flower-clock', 'Horloge Fleurie', "
        "'superseded', '[]', 'channel', '[]', datetime('now'), '[\"reconciliation\"]')")
    await _seed_claim(db, "claim-recon-new", "location-flower-clock", "location_type",
                      value="landmark")

    assert await recently_touched_entity_ids(db, limit=10) == []


# ---------------------------------------------------------------------------
# 2. Reconciliation no-op guards
# ---------------------------------------------------------------------------

async def test_recon_alias_equal_to_display_name_is_skipped(db):
    """The exact churn pattern from production: alias/name claims restating
    the entity's display name, later retracted by another pass."""
    from server.services.memory.reconciliation import make_reconciliation_tools

    await _seed_entity(db, "location-vallorcine", "location", "Vallorcine")
    tools = {t.name: t for t in make_reconciliation_tools(db)}

    out = await tools["add_claim"].handler(
        subject_id="location-vallorcine", claim_type_key="alias",
        value="Vallorcine")
    assert "no-op" in out.lower()
    # Case/whitespace-insensitive too.
    out = await tools["add_claim"].handler(
        subject_id="location-vallorcine", claim_type_key="name",
        value="  vallorcine ")
    assert "no-op" in out.lower()
    assert await db.fetch_one(
        "SELECT 1 FROM memory_claims WHERE subject_id = 'location-vallorcine' "
        "AND claim_type_key IN ('alias', 'name')") is None

    # A genuinely different alias still writes.
    out = await tools["add_claim"].handler(
        subject_id="location-vallorcine", claim_type_key="alias",
        value="Vallorcine (Chamonix)")
    assert "Added" in out
    assert await db.fetch_one(
        "SELECT 1 FROM memory_claims WHERE subject_id = 'location-vallorcine' "
        "AND claim_type_key = 'alias' AND value = 'Vallorcine (Chamonix)'") is not None


async def test_recon_supersede_same_value_is_skipped(db):
    """Rewriting a claim to the value it already holds mints a fresh row and
    re-enrols the entity — pure churn, refuse it."""
    from server.services.memory.reconciliation import make_reconciliation_tools

    await _seed_entity(db, "person-brian", "person", "Brian")
    await _seed_claim(db, "claim-x1", "person-brian", "interest",
                      value="likes family trip updates")
    tools = {t.name: t for t in make_reconciliation_tools(db)}

    out = await tools["supersede_claim_tool"].handler(
        subject_id="person-brian", claim_type_key="interest",
        old_value="likes family trip updates",
        new_value="Likes family trip updates ")  # same after normalisation
    assert "skipped" in out.lower()
    rows = await db.fetch_all(
        "SELECT * FROM memory_claims WHERE subject_id = 'person-brian' "
        "AND claim_type_key = 'interest'")
    assert len(rows) == 1 and rows[0]["status"] == "active"
    assert rows[0]["id"] == "claim-x1", "original row untouched, no replacement"

    # A real value change still goes through.
    out = await tools["supersede_claim_tool"].handler(
        subject_id="person-brian", claim_type_key="interest",
        old_value="likes family trip updates",
        new_value="prefers short trip digests")
    assert "Superseded" in out
    assert await db.fetch_one(
        "SELECT 1 FROM memory_claims WHERE subject_id = 'person-brian' "
        "AND claim_type_key = 'interest' AND value = 'prefers short trip digests' "
        "AND status = 'active'") is not None


async def test_recon_create_entity_batch_skips_noop_claims(db):
    from server.services.memory.reconciliation import make_reconciliation_tools

    tools = {t.name: t for t in make_reconciliation_tools(db)}
    claims = json.dumps([
        {"claim_type_key": "alias", "value": "Flower Clock"},
        {"claim_type_key": "location_type", "value": "landmark"},
    ])
    out = await tools["create_entity"].handler(
        entity_id="location-flower-clock", entity_type="location",
        claims_json=claims)
    assert "1 claims" in out, "alias==display name skipped, location_type written"
    assert await db.fetch_one(
        "SELECT 1 FROM memory_claims WHERE subject_id = 'location-flower-clock' "
        "AND claim_type_key = 'alias'") is None
    assert await db.fetch_one(
        "SELECT 1 FROM memory_claims WHERE subject_id = 'location-flower-clock' "
        "AND claim_type_key = 'location_type' AND value = 'landmark'") is not None


# ---------------------------------------------------------------------------
# 3. Claim routing double-fire
# ---------------------------------------------------------------------------

async def test_replay_does_not_reroute_inline_delivered_batch(ctx, db, mock_wake, llm_chat):
    """Inline delivery + watermark replay of the same batch: one routing-log
    row, and the probe LLM is not called a second time."""
    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_entity(db, "person-alice", "person")
    await _seed_message(db, "msg-extr-d1", GROUP_KEY)
    await _seed_claim(db, "claim-d1", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-d1"])

    llm_chat.side_effect = [
        json.dumps({"verdict": "RELEVANT"}),   # probe
        _reviser_json({"plan": "negotiate"}),
    ]
    result = await _run_batch(ctx, marker="msg-extr-d1")
    assert result["delivered"] == 1
    assert llm_chat.await_count == 2
    rows = await db.fetch_all(
        "SELECT * FROM memory_routing_log WHERE stimulus_id = 'msg-extr-d1'")
    assert len(rows) == 1

    from server.services.memory.claim_router import replay_pending
    replayed = await replay_pending(ctx)
    assert replayed == 1, "event replayed from the watermark"
    rows = await db.fetch_all(
        "SELECT * FROM memory_routing_log WHERE stimulus_id = 'msg-extr-d1'")
    assert len(rows) == 1, "replay skips the already-routed (goal, stimulus)"
    assert llm_chat.await_count == 2, "no second probe/reviser call"


async def test_failed_routing_stays_retryable(ctx, db, mock_wake, llm_chat):
    """A prior routing row that ended in an enqueue error must not block the
    redelivery the watermark replay exists to provide."""
    from server.services.memory.claim_router import _batch_for_turn, _route_batch

    await _seed_goal_with_entity_ref(ctx, "event-team-lunch")
    await _seed_entity(db, "event-team-lunch")
    await _seed_entity(db, "person-alice", "person")
    await _seed_message(db, "msg-extr-d2", GROUP_KEY)
    await _seed_claim(db, "claim-d2", "event-team-lunch", "attendee",
                      object_id="person-alice", message_ids=["msg-extr-d2"])
    await db.execute(
        "INSERT INTO memory_routing_log "
        "(id, stimulus_id, source_conversation_id, goal_id, claim_ids, entity_ids, "
        " match_type, probe_verdict, revise_outcome, wake_decision, detail, created_at) "
        "VALUES ('rl-err', 'msg-extr-d2', ?, (SELECT id FROM goals LIMIT 1), "
        "'[]', '[]', 'ref', 'relevant', 'error', 'pending', 'enqueue failed', "
        "datetime('now'))",
        (GROUP_KEY,))

    llm_chat.side_effect = [
        json.dumps({"verdict": "RELEVANT"}),   # probe
        _reviser_json({"plan": "retry"}),
    ]
    batch = await _batch_for_turn(db, "msg-extr-d2")
    result = await _route_batch(ctx, session_key=GROUP_KEY, cid=GROUP_KEY,
                                turn_message_id="msg-extr-d2", batch=batch)
    assert result["delivered"] == 1
    rows = await db.fetch_all(
        "SELECT * FROM memory_routing_log WHERE stimulus_id = 'msg-extr-d2'")
    assert len(rows) == 2, "error row did not block redelivery"


# ---------------------------------------------------------------------------
# 4. Extraction soft resolution + prompt
# ---------------------------------------------------------------------------

async def test_fuzzy_soft_resolution_catches_naming_drift(ctx, db):
    """task-nuffy-talkback-afl vs existing task-nuffy-talkback-segment — the
    exact-match rule let this through in production; token overlap steers
    the reuse instead of minting a near-duplicate entity."""
    from server.services.memory.extraction_tools import make_extraction_tools

    await _seed_entity(db, "task-nuffy-talkback-segment", "task",
                       "Nuffy Talkback Segment")
    tools = {t.name: t for t in make_extraction_tools(db, "msg-extr-x")}
    out = await tools["create_entity"].handler(
        entity_id="task-nuffy-talkback-afl", entity_type="task")
    assert "task-nuffy-talkback-segment" in out and "reuse" in out.lower()
    assert await db.fetch_one(
        "SELECT 1 FROM memory_entities WHERE entity_id = 'task-nuffy-talkback-afl'") is None


async def test_fuzzy_soft_resolution_keeps_dated_entities_apart(ctx, db):
    """Two Saturday-cartoons events on different dates are different things."""
    from server.services.memory.extraction_tools import make_extraction_tools

    await _seed_entity(db, "event-radio-cartoons-2026-09-12", "event",
                       "Bobs Pirate Radio Saturday Cartoons 2026 09 12")
    tools = {t.name: t for t in make_extraction_tools(db, "msg-extr-x")}
    out = await tools["create_entity"].handler(
        entity_id="event-radio-cartoons-2026-09-13", entity_type="event")
    assert "Created entity" in out
    assert await db.fetch_one(
        "SELECT 1 FROM memory_entities WHERE entity_id = 'event-radio-cartoons-2026-09-13'"
    ) is not None


def test_fuzzy_name_match_unit_cases():
    from server.services.memory.extraction_tools import _fuzzy_name_match

    # Same thing, naming drift (production near-duplicates).
    assert _fuzzy_name_match("Nuffy Talkback Afl", "Nuffy Talkback Segment")
    assert _fuzzy_name_match("Rotto Mike Weekend",
                             "Mike Rotto Weekend Sep2026")
    # Differently-dated entities stay separate.
    assert not _fuzzy_name_match("Cartoons 2026 09 12", "Cartoons 2026 09 13")
    # Single shared token is not enough.
    assert not _fuzzy_name_match("Mike Cleaver", "Mike Rotto")
    # No overlap.
    assert not _fuzzy_name_match("Paris", "Vallorcine")


def test_silent_turn_prompt_warns_against_hub_entity_journaling():
    from server.services.memory.prompts import build_silent_turn_prompt

    prompt = build_silent_turn_prompt("<claim types>", bot_name="Bob")
    assert "hub entities" in prompt
    assert "limit" in prompt and "shared_context" in prompt
    assert "event-log" in prompt or "journal" in prompt.lower()
