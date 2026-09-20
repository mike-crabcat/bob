"""Widened list_goals (2026-09-19 evening incident): the operator ruled on
the merch goal in their DM and the turn answered "no parent goal to inform"
— holder-scoped visibility made the goal invisible exactly where the
operator was talking about it.

list_goals now has two sources:
- relation=held: goals the conversation works/holds (unchanged)
- relation=origin-shared: active goals whose ORIGIN is a conversation
  sharing a human participant with this one (participant-overlap scoped:
  rooms and subagent sessions have no human overlap, so the widening is a
  no-op there)
"""

from __future__ import annotations

import json

from server.services import goal_service
from server.services.goal_tools import make_goal_tools

DM = "agent:main:whatsapp:dm:61411112222"
GROUP = "agent:main:whatsapp:group:777"


async def _seed_shared_contact(ctx, contact_id="c-shared") -> None:
    await ctx.db.execute(
        "INSERT OR IGNORE INTO contacts (id, name, phone_number, created_at, "
        "updated_at) VALUES (?, 'Shared Person', '+61400000111', "
        "datetime('now'), datetime('now'))", (contact_id,))
    from server.repositories.participants import ParticipantRepository
    repo = ParticipantRepository(ctx.db)
    now = "2026-09-19T00:00:00+00:00"
    await repo.upsert(DM, "+61411112222", display_name="Shared Person",
                      contact_id=contact_id, is_trusted=True, now_iso=now)
    await repo.upsert(GROUP, "+61400000111", display_name="Shared Person",
                      contact_id=contact_id, is_trusted=True, now_iso=now)


async def test_dm_sees_goal_originated_in_shared_group(ctx):
    await _seed_shared_contact(ctx)
    # Goal created with its origin in the shared group; a room goal so the
    # working conversation moves to the room (the DM holds nothing).
    goal = await goal_service.create_goal(
        ctx, conversation_id=GROUP, objective="merch 500",
        kind="task", strategy={"v": 2, "refs": {"entities": [], "claims": []}})

    tools = {t.name: t.handler for t in make_goal_tools(ctx, DM)}
    out = json.loads(await tools["list_goals"]())
    ids = {g["goal_id"]: g for g in out["goals"]}
    assert goal["id"] in ids
    assert ids[goal["id"]]["relation"] == "origin-shared"


async def test_room_session_not_widened(ctx):
    """A room has no human participants — participant overlap is empty, so
    list_goals in a room shows only its own/held goals (no flooding)."""
    await _seed_shared_contact(ctx)
    goal = await goal_service.create_goal(
        ctx, conversation_id=GROUP, objective="merch 500",
        kind="task", strategy={"v": 2, "refs": {"entities": [], "claims": []}})
    room = goal["conversation_id"]

    tools = {t.name: t.handler for t in make_goal_tools(ctx, room)}
    out = json.loads(await tools["list_goals"]())
    for g in out["goals"]:
        assert g["relation"] == "held"


async def test_unrelated_dm_sees_nothing(ctx):
    await _seed_shared_contact(ctx)
    await goal_service.create_goal(
        ctx, conversation_id=GROUP, objective="merch 500",
        kind="task", strategy={"v": 2, "refs": {"entities": [], "claims": []}})
    tools = {t.name: t.handler
             for t in make_goal_tools(ctx, "agent:main:whatsapp:dm:6499999")}
    out = json.loads(await tools["list_goals"]())
    assert out["goals"] == []
