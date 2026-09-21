"""Goal rooms (docs/goal-rooms-plan.md) — the plan's named invariants:

- D1/D13: qualifying kinds get a room + check-in series at creation; no room
  without a next check-in; wrapper kinds stay legacy
- D2/D14: room_state writes the state block; prose/bare dues rejected at the
  write
- D4/D5/D6: claim sensations tiered action(info)/coalesced per entity per
  turn; room-origin batches skipped whole; extraction candidates exclude
  goal rooms
- D7/D8: subscribe enforces source allowlist + ceilings + route cap; seeded
  routes come enabled
- D10: child settle rolls up as a direct wake of the parent ROOM
- D11: settle prunes the room's routes; parent cancel cascades to children
- D12: room_close refuses without evidence and with open children
- D15: dream plan approval seeds a room and writes dream_plans.task_id
- kill switch: BOB_GOAL_ROOMS=off → legacy path (no room, no emission)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from server.repositories.goals import GoalRepository
from server.repositories.stimulus import StimulusRepository
from server.repositories.wakeups import WakeupRepository
from server.services import goal_rooms, goal_service

ORIGIN = "agent:main:whatsapp:group:120363422982048691"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- creation

async def test_task_goal_gets_room_and_checkin(ctx):
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="obtain WFH hours",
        kind="task", deadline=_iso(datetime.now(timezone.utc) + timedelta(days=3)),
        strategy={"v": 2, "refs": {"entities": [], "claims": []}})
    room = goal_rooms.room_session_key(goal["id"])
    assert goal["conversation_id"] == room

    # Utility conversation row (charter) + report_to the origin.
    from server.repositories.utility_conversations import (
        UtilityConversationRepository,
    )
    row = await UtilityConversationRepository(ctx.db).get(room)
    assert row is not None and row["enabled"]
    assert "obtain WFH hours" in row["charter"]
    assert row["report_to"] == ORIGIN

    # D13: a check-in wakeup series exists, targeting the room.
    checkins = [w for w in await WakeupRepository(ctx.db).list_scheduled(room)
                if w["kind"] == "goal_checkin"]
    assert checkins and checkins[0]["recurrence"]

    # Holders: worker=room, origin=asking conversation.
    holders = {h["role"]: h["conversation_id"]
               for h in await GoalRepository(ctx.db).holders_of(goal["id"])}
    assert holders["worker"] == room
    assert holders["origin"] == ORIGIN


async def test_wrapper_kind_stays_legacy(ctx):
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="wrap a subagent",
        kind="subagent")
    assert not goal_rooms.is_room_session(goal["conversation_id"])


async def test_due_action_and_deadline_wakes_target_the_room(ctx):
    """Deadlines follow brains: the deadline wakeup lands in the ROOM
    (which carries the goal tools), never the origin. (The due-action
    sweep half retired with Phase 6b, 2026-09-22.)"""
    from datetime import datetime as _dt

    deadline = _iso(_dt.now(timezone.utc) + timedelta(days=2))
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="dated goal", kind="task",
        deadline=deadline,
        strategy={"v": 2, "next_actions": [
            {"action": "do the thing", "due": deadline}]})
    room = goal["conversation_id"]
    wake_repo = WakeupRepository(ctx.db)
    deadline_wakes = [w for w in await wake_repo.list_scheduled(room)
                      if w["kind"] == "wake" and w["goal_id"] == goal["id"]]
    assert deadline_wakes and all(
        w["conversation_id"] == room for w in deadline_wakes)
    assert not await wake_repo.list_scheduled(ORIGIN)


async def test_kill_switch_off_is_legacy(monkeypatch, ctx):
    monkeypatch.setattr(ctx.settings.goal_rooms, "enabled", False)
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="no room", kind="task")
    assert goal["conversation_id"] == ORIGIN


# ------------------------------------------------------------- room_state

async def _roomed_goal(ctx, **kw):
    return await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective=kw.pop("objective", "test goal"),
        kind="task", **kw)


def _tools(ctx, room):
    return {t.name: t.handler for t in goal_rooms.make_goal_room_tools(ctx, room)}


async def test_room_state_write_and_due_validation(ctx):
    goal = await _roomed_goal(ctx)
    room = goal["conversation_id"]
    tools = _tools(ctx, room)

    good = {"plan": "chase people", "known": ["a"], "open_questions": [],
            "next_actions": [{"action": "DM David",
                              "due": "2026-09-20T09:00:00+08:00"}],
            "refs": {"entities": ["person-david-shedden"], "claims": []}}
    res = json.loads(await tools["room_state"](json.dumps(good)))
    assert res["ok"], res
    stored = json.loads((await GoalRepository(ctx.db).get(goal["id"]))["strategy_json"])
    assert stored["next_actions"][0]["action"] == "DM David"

    # D14: prose/bare dues rejected at the write.
    bad = dict(good, next_actions=[{"action": "x", "due": "tomorrow morning"}])
    res = json.loads(await tools["room_state"](json.dumps(bad)))
    assert not res["ok"] and "ISO" in res["error"]

    bad_bare = dict(good, next_actions=[{"action": "x", "due": "2026-09-20"}])
    res = json.loads(await tools["room_state"](json.dumps(bad_bare)))
    assert not res["ok"]


async def test_room_turn_tools_add_outreach_only_when_bridge_up(ctx):
    """The room's hands match its mandate: with the bridge connected the DM
    outreach tool is present (chase individuals directly); disconnected it is
    absent, and group-send tools are NEVER included (broadcasts keep the
    origin routing + approval gate)."""
    class _Bridge:
        connected = False
    ctx.whatsapp_bridge = _Bridge()
    names_down = {t.name for t in goal_rooms.room_turn_tools(ctx, "agent:goal-x:utility")}
    assert "send_whatsapp_to_contact" not in names_down
    assert not any(n.startswith("send_to_group") or "group_send" in n
                   for n in names_down)
    assert {"room_state", "room_close", "room_spawn", "subscribe",
            "unsubscribe", "list_subscriptions"} <= names_down

    ctx.whatsapp_bridge.connected = True
    names_up = {t.name for t in goal_rooms.room_turn_tools(ctx, "agent:goal-x:utility")}
    assert "send_whatsapp_to_contact" in names_up
    assert "get_contact_session_messages" in names_up
    assert not any("group" in n for n in names_up - names_down)
    ctx.whatsapp_bridge = None



async def test_read_group_history_is_group_scoped(ctx):
    tools = _tools(ctx, "agent:goal-x:utility")
    assert "read_group_history" in tools
    res = json.loads(await tools["read_group_history"](
        "agent:main:whatsapp:dm:61480346372"))
    assert not res["ok"] and "group" in res["error"]
    res = json.loads(await tools["read_group_history"]("nonsense"))
    assert not res["ok"]


async def test_outreach_resolves_contacts_by_name(ctx):
    """Rooms hold no contact-id roster — send_whatsapp_to_contact must
    resolve a plain name (the 2026-09-17 merch-room stall: DMs to 'Rupert
    Quekett' bounced 'Contact not found' though the contact exists)."""
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c-rupert', 'Rupert Quekett', '+61424193179', ?, ?)",
        (_iso(datetime.now(timezone.utc)),) * 2)
    from server.repositories.contacts import ContactRepository
    assert (await ContactRepository(ctx.db).search_by_name("%Rupert%"))["id"] == "c-rupert"

    class _Bridge:
        connected = False
    from server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
    tools = {t.name: t.handler for t in make_whatsapp_outreach_tools(
        ctx, _Bridge(), "agent:goal-x:utility")}
    # Name resolves (gets as far as the bridge check — NOT contact-not-found).
    res = json.loads(await tools["send_whatsapp_to_contact"](
        "Rupert Quekett", "test", objective="test"))
    assert "Contact not found" not in json.dumps(res)
    assert not res["ok"] and "not connected" in res["error"]
    # Unknown name still fails cleanly.
    res = json.loads(await tools["send_whatsapp_to_contact"](
        "Nobody Here", "test", objective="test"))
    assert not res["ok"] and res["error"] == "Contact not found"


# ------------------------------------------------------------ sensations

def _batch(*claims, turn="t1"):
    return {"claims": list(claims), "claim_ids": [c["id"] for c in claims],
            "entity_ids": sorted({c["subject_id"] for c in claims})}


async def test_sensation_tiering_and_coalescing(ctx):
    turn = "msg-turn-1"
    batch = _batch(
        {"id": "c1", "claim_type_key": "preference", "subject_id": "person-david-shedden",
         "object_id": "", "value": "likes steak"},
        {"id": "c2", "claim_type_key": "work_schedule", "subject_id": "person-chris",
         "object_id": "", "value": "WFH Thursdays"},
        turn=turn)
    out = await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id=turn, batch=batch)
    assert out["emitted"] == 2 and out["action"] == 0 and out["info"] == 2

    repo = StimulusRepository(ctx.db)
    pend = {e["type"]: e for e in await repo.pending_events()}
    assert pend["claim.write.person-david-shedden"]["level"] == "info"
    assert pend["claim.write.person-david-shedden"]["summary"].startswith(
        "person-david-shedden preference")

    # D5 coalescing: re-emit of the same turn+entity is a no-op.
    out2 = await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id=turn, batch=batch)
    assert out2["emitted"] == 0


async def test_sensation_correction_is_action(ctx):
    # An existing claim superseded by this batch's new claim → action level.
    await ctx.db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, object_id, "
        "value, status, created_at, superseded_by) "
        "VALUES ('old1', 'work_schedule', 'person-chris', NULL, "
        "'Thursdays', 'superseded', ?, '[\"c9\"]')",
        (_iso(datetime.now(timezone.utc)),))
    batch = _batch({"id": "c9", "claim_type_key": "work_schedule",
                    "subject_id": "person-chris", "object_id": "",
                    "value": "full-time now"})
    out = await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id="t2", batch=batch)
    assert out["action"] == 1
    repo = StimulusRepository(ctx.db)
    [ev] = [e for e in await repo.pending_events()
            if e["type"] == "claim.write.person-chris"]
    assert ev["level"] == "action"


async def test_self_echo_room_batches_skipped(ctx):
    batch = _batch({"id": "c1", "claim_type_key": "preference",
                    "subject_id": "person-david-shedden", "object_id": "",
                    "value": "x"})
    room_batch = await goal_rooms.emit_claim_sensations(
        ctx, session_key="agent:goal-abc:utility", turn_message_id="t3",
        batch=batch)
    assert room_batch["skipped_room"] and room_batch["emitted"] == 0


async def test_extraction_candidates_exclude_goal_rooms(ctx):
    from server.repositories.history import HistoryRepository
    from server.services.session_service import SessionService

    room = "agent:goal-xyz:utility"
    await ctx.db.execute(
        "INSERT INTO conversations (id, kind, created_at, updated_at) "
        "VALUES (?, 'utility', ?, ?)",
        (room, _iso(datetime.now(timezone.utc)),
         _iso(datetime.now(timezone.utc))))
    await SessionService(ctx).add_message(room, "assistant", "room narration")

    rows = await HistoryRepository(ctx.db).extraction_candidates(
        idle_threshold_minutes=0)
    keys = {r["session_key"] for r in rows}
    assert room not in keys


# ---------------------------------------------------------- subscriptions

async def test_subscribe_allowlist_ceilings_and_cap(ctx):
    goal = await _roomed_goal(
        ctx, strategy={"v": 2, "refs": {
            "entities": ["person-david-shedden", "person-chris"], "claims": []}})
    room = goal["conversation_id"]
    tools = _tools(ctx, room)

    # D8: seeded routes exist and are enabled.
    seeded = json.loads(await tools["list_subscriptions"]())
    assert len(seeded["subscriptions"]) == 2
    assert all(s["enabled"] for s in seeded["subscriptions"])
    assert all(s["cooldown_s"] >= goal_rooms.MIN_COOLDOWN_S for s in
               seeded["subscriptions"])

    # D7: source allowlist.
    res = json.loads(await tools["subscribe"]("claim.write.x", source="frigate"))
    assert not res["ok"] and "memory" in res["error"]

    # D7: route cap.
    for i in range(20):
        await tools["subscribe"](f"claim.write.entity-{i}")
    res = json.loads(await tools["subscribe"]("claim.write.one-more"))
    assert not res["ok"] and "cap" in res["error"]

    # unsubscribe prunes; re-subscribe re-enables rather than duplicating.
    subs = json.loads(await tools["list_subscriptions"]())
    first = [s for s in subs["subscriptions"]
             if s["pattern"] == "claim.write.person-david-shedden"][0]
    assert json.loads(await tools["unsubscribe"](first["route_id"]))["ok"]
    res = json.loads(await tools["subscribe"]("claim.write.person-david-shedden"))
    assert res["ok"]
    resub = json.loads(await tools["list_subscriptions"]())
    matching = [s for s in resub["subscriptions"]
                if s["pattern"] == "claim.write.person-david-shedden"]
    assert len(matching) == 1


# ------------------------------------------------------ routing + rollup

async def test_room_gets_claim_via_router_not_reviser(ctx):
    """An action-level claim sensation steers the subscribed ROOM through the
    stimulus router — and the legacy claim router skips room goals."""
    from server.services import stimulus_router

    goal = await _roomed_goal(
        ctx, strategy={"v": 2, "refs": {"entities": ["person-chris"],
                                        "claims": []}})
    room = goal["conversation_id"]

    await ctx.db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, object_id, "
        "value, status, created_at, superseded_by) "
        "VALUES ('old1', 'work_schedule', 'person-chris', NULL, "
        "'Thursdays', 'superseded', ?, '[\"c9\"]')",
        (_iso(datetime.now(timezone.utc)),))
    batch = _batch({"id": "c9", "claim_type_key": "work_schedule",
                    "subject_id": "person-chris", "object_id": "",
                    "value": "full-time now"})
    await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id="t9", batch=batch)

    steered = []
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(side_effect=lambda c, target, content, **kw:
                             steered.append(target) or True)):
        await stimulus_router.drain(ctx)
    assert room in steered


async def test_child_settle_wakes_parent_room_directly(ctx):
    parent = await _roomed_goal(ctx, objective="parent")
    tools = _tools(ctx, parent["conversation_id"])
    child = json.loads(await tools["room_spawn"]("child objective"))
    assert child["ok"]
    child_goal = await GoalRepository(ctx.db).get(child["goal_id"])
    assert goal_rooms.is_room_session(child_goal["conversation_id"])

    wakes = []
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(side_effect=lambda c, target, content, **kw:
                             wakes.append(target) or True)):
        await goal_service.settle_goal(
            ctx, child["goal_id"], status="completed", result="did it")
    # D10: the parent ROOM was woken directly (no reviser enqueue).
    assert parent["conversation_id"] in wakes


# ------------------------------------------------------------ close door

async def test_close_door_evidence_and_children(ctx):
    goal = await _roomed_goal(ctx, objective="closeable")
    room = goal["conversation_id"]
    tools = _tools(ctx, room)

    # D12: no evidence → refused.
    res = json.loads(await tools["room_close"](result="done"))
    assert not res["ok"] and "evidence" in res["error"]

    # Open child → refused.
    await tools["room_spawn"]("child blocks close")
    res = json.loads(await tools["room_close"](result="done", evidence="msg 1"))
    assert not res["ok"] and "child" in res["error"]

    # Settle the child, then close lands with evidence recorded.
    child = (await GoalRepository(ctx.db).children_of(goal["id"]))[0]
    await goal_service.settle_goal(ctx, child["id"], status="completed",
                                   result="child done")
    wakes = []
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(side_effect=lambda c, target, content, **kw:
                             wakes.append(target) or True)):
        res = json.loads(await tools["room_close"](result="done",
                                                   evidence="msg 1, claim 2"))
    assert res["ok"]
    row = await GoalRepository(ctx.db).get(goal["id"])
    assert row["status"] == "completed"
    assert "Evidence: msg 1, claim 2" in row["result"]


async def test_settle_prunes_routes_and_cancel_cascades(ctx):
    parent = await _roomed_goal(
        ctx, objective="parent", strategy={"v": 2, "refs": {
            "entities": ["person-david-shedden"], "claims": []}})
    room = parent["conversation_id"]
    tools = _tools(ctx, room)
    child = json.loads(await tools["room_spawn"]("child"))
    child_room = child["room"]

    s_repo = StimulusRepository(ctx.db)
    before = [r for r in await s_repo.routes(enabled_only=True)
              if r["target_session"] == room]
    assert before  # seeded subscription present

    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)):
        await goal_service.settle_goal(ctx, parent["id"], status="cancelled",
                                       result="called off")
    # D11: parent routes pruned, child cascaded, child room's check-in dead.
    after = [r for r in await s_repo.routes(enabled_only=True)
             if r["target_session"] == room]
    assert not after
    child_row = await GoalRepository(ctx.db).get(child["goal_id"])
    assert child_row["status"] == "cancelled"
    assert child_row["conversation_id"] == child_room


async def test_hygiene_sweep_prunes_orphan_routes(ctx):
    goal = await _roomed_goal(
        ctx, strategy={"v": 2, "refs": {"entities": ["person-chris"],
                                        "claims": []}})
    room = goal["conversation_id"]
    await goal_service.settle_goal(
        ctx, goal["id"], status="completed", result="done",
        wake_origin=False)
    # Simulate a settle that skipped pruning (manual SQL, crashed prune).
    s_repo = StimulusRepository(ctx.db)
    rid = await s_repo.insert_route(
        source="memory", type_pattern="claim.write.person-chris",
        level="action", target_session=room, created_by="goal_rooms")
    await s_repo.set_route_enabled(rid, True)
    assert await goal_rooms.prune_orphan_routes(ctx) >= 1
    assert await s_repo.get_route(rid) is None


# ---------------------------------------------------------------- check-in

async def test_checkin_carries_full_claim_activity(ctx):
    """The check-in brief lists EVERY claim event in the window — ✓ for
    followed entities, · for unfollowed — plus subscribe candidates and
    quiet subscriptions. The room sees what went past, not just what it
    caught (operator ask 2026-09-17)."""
    goal = await _roomed_goal(
        ctx, strategy={"v": 2, "refs": {"entities": ["person-chris"],
                                        "claims": []}})
    room = goal["conversation_id"]

    followed_batch = _batch({"id": "c1", "claim_type_key": "preference",
                             "subject_id": "person-chris", "object_id": "",
                             "value": "prefers mornings"})
    other_batch = _batch({"id": "c2", "claim_type_key": "preference",
                          "subject_id": "person-uneardent", "object_id": "",
                          "value": "collects stamps"})
    await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id="t-a", batch=followed_batch)
    await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id="t-b", batch=other_batch)
    repo = StimulusRepository(ctx.db)
    await repo.mark_processed(
        [e["id"] for e in await repo.pending_events()], "log-only")

    result = await goal_rooms.render_checkin(ctx, goal)
    assert "prefers mornings" in result          # followed event listed
    assert "collects stamps" in result           # unfollowed event listed
    assert "✓" in result and "·" in result       # both marks present
    assert "person-uneardent" in result          # subscribe candidate named
    # Quiet list: seeded routes with no events (person-chris fired, so at
    # least one seeded quiet entry... none seeded beyond chris — the room
    # seeded only chris; quiet may be empty here, mark list header absent.
    assert ("no events in the window" in result) == bool(
        [r for r in await repo.routes_for_target(room)
         if r["type_pattern"] != "claim.write.person-chris"])


async def test_emission_skips_self_and_relationship_noise(ctx):
    """self-bob / relationship-bob claims never become sensations — 25% of
    the live stream had no possible consumer (self material feeds the
    self-brief, not goal attention)."""
    batch = _batch(
        {"id": "s1", "claim_type_key": "self_state", "subject_id": "self-bob",
         "object_id": "", "value": "running fine"},
        {"id": "s2", "claim_type_key": "connection",
         "subject_id": "relationship-bob-mike-cleaver", "object_id": "",
         "value": "x"},
        {"id": "s3", "claim_type_key": "preference",
         "subject_id": "person-mike-cleaver", "object_id": "", "value": "y"})
    out = await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id="t-noise", batch=batch)
    assert out["emitted"] == 1  # only person-mike-cleaver
    types = [e["type"] for e in await StimulusRepository(ctx.db).pending_events()]
    assert types == ["claim.write.person-mike-cleaver"]


# ----------------------------------------------------------------- dream D15

async def test_dream_approval_no_longer_seeds_room(ctx):
    """D15 retired (Mike 2026-09-20): the dream PROPOSES, the announcement
    ASKS, and only the PEOPLE'S REPLY raises a goal. Approval — operator or
    auto — spawns no machinery."""
    from server.services.dream.store import DreamStore

    store = DreamStore(ctx)
    await ctx.db.execute(
        "INSERT INTO dream_runs (id, started_at, window_start, window_end, "
        "status, trigger, model) VALUES ('dream-1', ?, ?, ?, 'complete', "
        "'cli', 'm')",
        (_iso(datetime.now(timezone.utc)),
         _iso(datetime.now(timezone.utc) - timedelta(days=1)),
         _iso(datetime.now(timezone.utc))))
    await ctx.db.execute(
        "INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, "
        "assistance_method, status, source_run_id, created_at, updated_at) "
        "VALUES ('plan-1', 'book the venue', 'talked about it', 'call them', "
        "'draft the message', 'draft', 'dream-1', ?, ?)",
        (_iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc))))

    await store.set_plan_status("plan-1", "approved", approved_by="auto")

    row = await ctx.db.fetch_one("SELECT task_id FROM dream_plans WHERE id = 'plan-1'")
    assert not row["task_id"]                    # nothing seeded at approval
    assert not await GoalRepository(ctx.db).list_active()  # no goal at all


async def test_dream_announcement_registers_offer_task(ctx):
    """The reply-commissioned path: announcing a plan registers an offer-task
    whose completer is the announced conversation — the reply turn creates
    the goal and settles the task; approval never did it."""
    from unittest.mock import MagicMock

    from server.services.dream.announce import AnnounceService

    await ctx.db.execute(
        "INSERT INTO dream_runs (id, started_at, window_start, window_end, "
        "status, trigger, model) VALUES ('dream-2', ?, ?, ?, 'complete', "
        "'cli', 'm')",
        (_iso(datetime.now(timezone.utc)),
         _iso(datetime.now(timezone.utc) - timedelta(days=1)),
         _iso(datetime.now(timezone.utc))))
    import json as _j
    await ctx.db.execute(
        "INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, "
        "assistance_method, status, source_run_id, approved_by, approved_at, "
        "evidence_json, created_at, updated_at) "
        "VALUES ('plan-2', 'sanrio gift', 'talked about it', 'follow up', "
        "'name the products', 'approved', 'dream-2', 'auto', ?, ?, ?, ?)",
        (_iso(datetime.now(timezone.utc)),
         _j.dumps([{"session_key": ORIGIN, "kind": "commitment",
                    "excerpt": "Helen asked for links", "run_id": "dream-2"}]),
         _iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc))))
    await ctx.db.execute(
        "INSERT INTO dream_item_links (item_type, item_id, session_key) "
        "VALUES ('plan', 'plan-2', ?)", (ORIGIN,))

    ctx.settings.dream.announce_factcheck = False  # no LLM in unit tests
    bridge = MagicMock()
    bridge.connected = True
    bridge.send_message = AsyncMock(return_value="req-1")
    ctx.whatsapp_bridge = bridge
    with patch.object(AnnounceService, "_compose",
                      new=AsyncMock(return_value="checking in!")):
        result = await AnnounceService(ctx).flush()
    ctx.whatsapp_bridge = None

    assert result["plans_announced"] == 1
    from server.repositories.tasks import TaskRepository
    repo = TaskRepository(ctx.db)
    owed = await repo.list_for_completer(ORIGIN)
    assert owed and owed[0]["expected_completer"] == ORIGIN
    assert "plan-2" in owed[0]["title"] and "awaiting their answer" in owed[0]["title"]
    import json as _json
    assert _json.loads(owed[0]["payload_json"]).get("dream_plan_id") == "plan-2"
    # No goal spawned by announcing.
    assert not await GoalRepository(ctx.db).list_active()

    # The reply turn: complete the offer task + create the goal.
    from server.services import goal_service
    from server.services.tasks import settle_task
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="sanrio gift follow-up",
        kind="task",
        strategy={"v": 2, "refs": {"entities": [], "claims": []}})
    out = await settle_task(
        ctx, owed[0]["id"], to_status="completed",
        result=f"reply expressed interest — goal {goal['id']} created",
        completed_by=f"conversation:{ORIGIN}")
    assert out["ok"]
    await ctx.db.execute(
        "UPDATE dream_plans SET task_id = ?, updated_at = ? WHERE id = 'plan-2'",
        (goal["id"], _iso(datetime.now(timezone.utc))))
    row = await ctx.db.fetch_one("SELECT task_id FROM dream_plans WHERE id = 'plan-2'")
    assert row["task_id"] == goal["id"]


# ------------------------------------------------------------------ adopt

async def test_adopt_legacy_goal(ctx):
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="legacy straggler",
        kind="task",
        strategy={"v": 2, "refs": {"entities": ["person-chris"], "claims": []}})
    # Undo the room (simulate a legacy-created goal).
    await ctx.db.execute(
        "UPDATE goals SET conversation_id = ? WHERE id = ?", (ORIGIN, goal["id"]))
    res = await goal_rooms.adopt_goal(ctx, goal["id"])
    assert res["ok"]
    row = await GoalRepository(ctx.db).get(goal["id"])
    assert row["conversation_id"] == res["room"]
    checkins = [w for w in await WakeupRepository(ctx.db).list_scheduled(res["room"])
                if w["kind"] == "goal_checkin"]
    assert checkins

    # Re-adoption is idempotent: no duplicate routes, no duplicate check-ins.
    s_repo = StimulusRepository(ctx.db)
    before = await s_repo.routes_for_target(res["room"])
    await goal_rooms.adopt_goal(ctx, goal["id"])
    after = await s_repo.routes_for_target(res["room"])
    assert len(after) == len(before)
    patterns = [r["type_pattern"] for r in after]
    assert len(patterns) == len(set(patterns))
    assert all(r["enabled"] for r in after)


async def test_room_outreach_auto_subscribes_engagement_route(ctx):
    """A room DMing a contact auto-subscribes to that person's entity: their
    replies extract as claims → sensations at the room, closing the
    engagement loop without waiting for the outreach goal to settle."""
    from unittest.mock import AsyncMock

    now = _iso(datetime.now(timezone.utc))
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c-rup', 'Rupert Quekett', '+61424193179', ?, ?)", (now, now))
    await ctx.db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name) "
        "VALUES ('person-rupert-quekett', 'person', 'Rupert Quekett')")

    room = "agent:goal-auto:utility"
    await goal_rooms.ensure_room(
        ctx, goal_id="auto", objective="engagement loop", kind="task",
        deadline=None, origin_session=ORIGIN)

    class _Bridge:
        connected = True
        send_message = AsyncMock(return_value="req-1")
        send_media = AsyncMock(return_value="req-2")
    from server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
    tools = {t.name: t.handler for t in make_whatsapp_outreach_tools(
        ctx, _Bridge(), room)}
    res = json.loads(await tools["send_whatsapp_to_contact"](
        "Rupert Quekett", "catalogue", objective="pitch merch"))
    assert res.get("ok"), res

    s_repo = StimulusRepository(ctx.db)
    routes = [r["type_pattern"] for r in await s_repo.routes_for_target(room)]
    assert "claim.write.person-rupert-quekett" in routes
    # Idempotent: a second send must not duplicate the engagement route.
    await tools["send_whatsapp_to_contact"](
        "Rupert Quekett", "follow-up", objective="pitch merch again")
    dupes = [r for r in await s_repo.routes_for_target(room)
             if r["type_pattern"] == "claim.write.person-rupert-quekett"]
    assert len(dupes) == 1 and dupes[0]["enabled"]
