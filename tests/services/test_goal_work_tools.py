"""Unified goal tools (commitments plan Phase 3, 2026-10-05).

Pins the v2 surface: access levels mirror the old trust split; outcomes
take batch promise children; closes are idempotent; one list shows
everything owed; the registry attaches each name once.
"""

from __future__ import annotations

import json

from server.repositories.goals import GoalRepository
from server.services.goal_work_tools import make_goal_work_tools

DM = "agent:main:whatsapp:dm:61400000002"
GROUP = "agent:main:whatsapp:group:120363000000000001"


def _tools(ctx, session_key, access):
    return {t.name: t for t in make_goal_work_tools(ctx, session_key, access=access)}


async def _call(tool, **kw):
    return json.loads(await tool.handler(**kw))


async def test_access_levels_shape_the_surface(ctx):
    full = _tools(ctx, DM, "full")
    assert {"add_goal", "close_goal", "list_goals", "delegate_goal",
            "schedule_goal", "update_goal_state"} <= set(full)
    create = _tools(ctx, GROUP, "create")
    assert "schedule_goal" not in create and "update_goal_state" not in create
    none = _tools(ctx, DM, "none")
    r = await _call(none["add_goal"], text="ship it", profile="outcome")
    assert not r["ok"] and "promise" in r["error"]
    r = await _call(none["add_goal"], text="send the deck", profile="promise")
    assert r["ok"] and r["goal_id"].startswith("prm-")


async def test_outcome_with_batch_children(ctx):
    t = _tools(ctx, DM, "full")
    kids = [{"text": f"figurine for {n}", "due_minutes": 1440}
            for n in ("Sam", "Alex", "Jo")]
    r = await _call(t["add_goal"], text="3 D&D figurines, approved STLs",
                    label="build", children=json.dumps(kids))
    assert r["ok"], r
    assert len(r["children"]) == 3 and all(c["ok"] for c in r["children"])
    goal = await GoalRepository(ctx.db).get(r["goal_id"])
    for c in r["children"]:
        row = await GoalRepository(ctx.db).get(c["goal_id"])
        assert row["kind"] == "promise"
        assert row["source_goal_id"] == r["goal_id"]
        assert row["origin_conversation_id"] == goal["conversation_id"], \
            "children are awaited by the outcome's room"


async def test_batch_children_under_existing_goal(ctx):
    """A room splits its OWN goal: parent_goal_id + children adds promises
    under it — no nested goal is created."""
    t = _tools(ctx, DM, "full")
    o = await _call(t["add_goal"], text="figurine set, 2 approved STLs",
                    label="build")
    before = len(await GoalRepository(ctx.db).list_active(limit=500))
    r = await _call(t["add_goal"], text="", parent_goal_id=o["goal_id"],
                    children=json.dumps([{"text": "Sam"}, {"text": "Jo"}]))
    assert r["ok"] and r["goal_id"] == o["goal_id"] and len(r["children"]) == 2
    assert len(await GoalRepository(ctx.db).list_active(limit=500)) == before, \
        "no new outcome goal"


async def test_bad_children_rejected_before_creating(ctx):
    t = _tools(ctx, DM, "full")
    r = await _call(t["add_goal"], text="x", children='[{"nope": 1}]')
    assert not r["ok"] and "children" in r["error"]


async def test_close_is_idempotent_for_promises_and_outcomes(ctx):
    t = _tools(ctx, DM, "full")
    p = await _call(t["add_goal"], text="call the venue", profile="promise")
    first = await _call(t["close_goal"], goal_id=p["goal_id"], result="booked")
    assert first["ok"] and first["status"] == "completed"
    again = await _call(t["close_goal"], goal_id=p["goal_id"], result="booked")
    assert again["ok"] and "already completed" in again["note"]

    o = await _call(t["add_goal"], text="plan the party, venue confirmed",
                    label="event_plan")
    c = await _call(t["close_goal"], goal_id=o["goal_id"], outcome="cancelled",
                    result="party called off")
    assert c["ok"]
    again = await _call(t["close_goal"], goal_id=o["goal_id"], outcome="cancelled")
    assert again["ok"] and "already cancelled" in again["note"]


async def test_create_access_cannot_close_outcomes(ctx):
    full = _tools(ctx, DM, "full")
    o = await _call(full["add_goal"], text="research venues, shortlist of 3",
                    label="research")
    group = _tools(ctx, GROUP, "create")
    r = await _call(group["close_goal"], goal_id=o["goal_id"])
    assert not r["ok"] and "owner" in r["error"]


async def test_list_goals_shows_everything_owed(ctx):
    t = _tools(ctx, DM, "full")
    await _call(t["add_goal"], text="send the slides", profile="promise")
    r = await _call(t["list_goals"])
    assert r["ok"] and any(w["title"] == "send the slides" for w in r["waiting_on"])
    assert "outcomes" in r and "expected_of_you" in r


async def test_delegate_requires_a_target(ctx):
    t = _tools(ctx, DM, "full")
    r = await _call(t["delegate_goal"], text="chase Sam", to="")
    assert not r["ok"] and "find_session" in r["error"]


async def test_registry_attaches_each_name_once(ctx):
    from server.services.tool_registry import build_common_tools
    for trusted, key, want_schedule in ((True, DM, True), (False, GROUP, False),
                                        (False, DM, False)):
        tools = build_common_tools(ctx, session_key=key, is_trusted=trusted)
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), "duplicate tool names"
        assert "add_goal" in names and "task_register" not in names
        assert ("schedule_goal" in names) is want_schedule


async def test_update_goal_without_expected_version(ctx):
    """flash omits expected_version (2026-10-05 evals): the write lands on
    the current version instead of failing a round; a stale explicit
    version is still rejected."""
    t = _tools(ctx, DM, "full")
    r = await _call(t["add_goal"], text="book the venue", label="event_plan")
    assert r["ok"], r
    gid = r["goal_id"]
    r = await _call(t["update_goal"], goal_id=gid, progress="David confirmed, gnome pose")
    assert r["ok"], r
    goal = await GoalRepository(ctx.db).get(gid)
    assert "gnome" in " ".join(v for v in goal.values() if isinstance(v, str))
    stale = await _call(t["update_goal"], goal_id=gid, progress="old news",
                        expected_version=0)
    assert not stale["ok"]
    r = await _call(t["update_goal"], goal_id="no-such-goal", progress="x")
    assert not r["ok"]


async def _room_goal(ctx, text="Build the card game; proof: design doc"):
    t = _tools(ctx, DM, "full")
    r = await _call(t["add_goal"], text=text, label="build", children=json.dumps([
        {"text": "Design doc"}, {"text": "Checkpoint: Mike approves prototype order"}]))
    assert r["ok"], r
    goal = await GoalRepository(ctx.db).get(r["goal_id"])
    return goal, r["children"]


async def test_children_default_to_a_day(ctx):
    _, kids = await _room_goal(ctx)
    from datetime import datetime, timezone
    due = datetime.fromisoformat(kids[0]["due"].replace("Z", "+00:00"))
    assert (due - datetime.now(timezone.utc)).total_seconds() > 20 * 3600


async def test_room_cannot_approve_its_own_checkpoint(ctx):
    """2026-10-06: the room closed 'Mike approves prototype order' itself."""
    goal, kids = await _room_goal(ctx)
    room = _tools(ctx, goal["conversation_id"], "full")
    approval = kids[1]["goal_id"]
    r = await _call(room["close_goal"], goal_id=approval, result="checkpoint reached")
    assert not r["ok"] and "delegate_goal" in r["error"]
    # Ask the origin; once it answers, the checkpoint can close.
    d = await _call(room["delegate_goal"], text="Mike: approve 54-card prototype ~US$20?",
                    to=DM)
    assert d["ok"], d
    asked = await GoalRepository(ctx.db).get(d["goal_id"])
    assert asked["source_goal_id"] == goal["id"], "room delegations file under its goal"
    origin = _tools(ctx, DM, "full")
    closed = await _call(origin["close_goal"], goal_id=d["goal_id"], result="yes, go")
    assert closed["ok"], closed
    assert (await _call(room["close_goal"], goal_id=approval, result="Mike approved"))["ok"]


async def test_only_the_completer_completes_a_promise(ctx):
    t = _tools(ctx, DM, "full")
    d = await _call(t["delegate_goal"], text="Get Sam's address", to=GROUP)
    r = await _call(t["close_goal"], goal_id=d["goal_id"], result="done")
    assert not r["ok"] and "only that conversation" in r["error"]
    assert (await _call(t["close_goal"], goal_id=d["goal_id"], outcome="cancelled",
                        result="not needed"))["ok"]


async def test_goal_creator_defaults_to_the_person_talking(ctx):
    from server.services.session_service import SessionService
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c-mike', 'Mike', '+61400009999', datetime('now'), datetime('now'))")
    await SessionService(ctx).add_message(GROUP, "user", "Do it! Unlimited budget.",
                                          sender_id="c-mike")
    t = _tools(ctx, GROUP, "full")
    r = await _call(t["add_goal"], text="Build the game; proof: doc", label="build")
    assert r["ok"], r
    assert (await GoalRepository(ctx.db).get(r["goal_id"]))["creator_contact_id"] == "c-mike"
