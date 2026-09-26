"""Goal creator pinning (2026-09-25) — the room inherits its creator's
principal.

DM-commissioned goals derive the creator from the DM binding (digit-
normalised phone match, same join as the 013 backfill); group goals carry
an explicit owner (Bob asks who owns it — passed via the create_goal tool's
owner param); children inherit the parent's creator; system goals stay
NULL and the room scopes down to untrusted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from server.repositories.goals import GoalRepository
from server.services import goal_rooms, goal_service

NOW = datetime.now(timezone.utc).isoformat()
DM_SESSION = "agent:main:whatsapp:dm:+61456224867"
GROUP_SESSION = "agent:main:whatsapp:group:120363401238199025"


async def _seed_contact(db, cid: str, name: str, phone: str,
                        trusted: int = 0) -> str:
    await db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_default, "
        "is_trusted, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
        (cid, name, phone, trusted, NOW, NOW))
    return cid


async def _seed_dm_binding(db, cid: str, session: str, address: str) -> None:
    """A 1:1 whatsapp DM: conversation + identity binding."""
    await db.execute(
        "INSERT INTO conversations (id, kind, title, created_at, updated_at) "
        "VALUES (?, 'dm', ?, ?, ?)", (cid, address, NOW, NOW))
    await db.execute(
        "INSERT INTO bindings (session_key, conversation_id, channel, kind, "
        "address, created_at) VALUES (?, ?, 'whatsapp', "
        "'identity', ?, ?)", (session, cid, address, NOW))


async def _goal(db, goal_id: str) -> dict:
    return await GoalRepository(db).get(goal_id)


# ------------------------------------------------------------ derivation

@pytest.mark.asyncio
async def test_dm_goal_pins_creator(ctx):
    await _seed_contact(ctx.db, "cid-mike", "Mike Cleaver",
                        "+61456224867", trusted=1)
    await _seed_dm_binding(ctx.db, "conv-dm", DM_SESSION, "+61456224867")

    goal = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="figurine doc by 5pm")
    assert goal["creator_contact_id"] == "cid-mike"


@pytest.mark.asyncio
async def test_dm_binding_address_formats_normalise(ctx):
    """The @s.whatsapp.net-suffixed and bare-number address variants must
    both match the contact's +E.164 phone."""
    await _seed_contact(ctx.db, "cid-a", "A", "+61411112222")
    await _seed_dm_binding(ctx.db, "conv-a",
                           "agent:main:whatsapp:dm:61411112222",
                           "61411112222@s.whatsapp.net")
    goal = await goal_service.create_goal(
        ctx, conversation_id="agent:main:whatsapp:dm:61411112222",
        objective="ob")
    assert goal["creator_contact_id"] == "cid-a"


@pytest.mark.asyncio
async def test_group_goal_without_owner_is_null(ctx):
    goal = await goal_service.create_goal(
        ctx, conversation_id=GROUP_SESSION, objective="ob")
    assert goal["creator_contact_id"] is None


@pytest.mark.asyncio
async def test_explicit_owner_wins_over_derivation(ctx):
    await _seed_contact(ctx.db, "cid-mike", "Mike Cleaver",
                        "+61456224867", trusted=1)
    await _seed_contact(ctx.db, "cid-sean", "Sean", "+61499990000")
    await _seed_dm_binding(ctx.db, "conv-dm", DM_SESSION, "+61456224867")

    goal = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="ob",
        creator_contact_id="cid-sean")
    assert goal["creator_contact_id"] == "cid-sean"


@pytest.mark.asyncio
async def test_child_inherits_parent_creator(ctx):
    await _seed_contact(ctx.db, "cid-mike", "Mike Cleaver",
                        "+61456224867", trusted=1)
    await _seed_dm_binding(ctx.db, "conv-dm", DM_SESSION, "+61456224867")
    parent = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="parent")

    child = await goal_service.create_goal(
        ctx, conversation_id="agent:main:whatsapp:group:120363401238199025",
        objective="child", parent_goal_id=parent["id"])
    assert child["creator_contact_id"] == "cid-mike"


# ------------------------------------------------------------ tool surface

@pytest.mark.asyncio
async def test_create_goal_tool_resolves_owner_by_name(ctx):
    await _seed_contact(ctx.db, "cid-chris", "Chris", "+61424616977")
    from server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, GROUP_SESSION)}
    out = json.loads(await tools["create_goal"].handler(
        "mockups approved by everyone", owner="Chris"))
    assert out["ok"] is True, out
    goal = await _goal(ctx.db, out["goal_id"])
    assert goal["creator_contact_id"] == "cid-chris"


@pytest.mark.asyncio
async def test_create_goal_tool_unknown_owner_errors(ctx):
    from server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, GROUP_SESSION)}
    out = json.loads(await tools["create_goal"].handler(
        "ob", owner="No Such Person"))
    assert out["ok"] is False and "no contact" in out["error"]


# ------------------------------------------------- room principal + scoping

@pytest.mark.asyncio
async def test_room_creator_principal_matrix(ctx):
    trusted = await _seed_contact(ctx.db, "cid-mike", "Mike",
                                  "+61456224867", trusted=1)
    member = await _seed_contact(ctx.db, "cid-david", "David",
                                 "+61401203022")
    g_owner = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="a",
        creator_contact_id=trusted)
    g_member = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="b",
        creator_contact_id=member)
    g_system = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="c")

    assert await goal_rooms.room_creator_principal(
        ctx, goal_rooms.room_session_key(g_owner["id"])) == (True, "cid-mike")
    assert await goal_rooms.room_creator_principal(
        ctx, goal_rooms.room_session_key(g_member["id"])) == (False, "cid-david")
    assert await goal_rooms.room_creator_principal(
        ctx, goal_rooms.room_session_key(g_system["id"])) == (False, None)


@pytest.mark.asyncio
async def test_room_scoping_via_creator_principal(ctx):
    """The wake-path composition: resolver output feeding
    make_group_lookup_tools — an owner-grade room sees any roster, a
    member-grade room only its creator's groups."""
    from server.repositories.groups import GroupRepository
    from server.services.group_tools import make_group_lookup_tools

    groups = GroupRepository(ctx.db)
    gid = await groups.upsert_group(
        "120363401238199025@g.us", name="AI doom", description="",
        member_count=1, now_iso=NOW)
    await groups.upsert_group("120363424924177634@g.us", name="Bob cast",
                              description="", member_count=0, now_iso=NOW)
    await _seed_contact(ctx.db, "cid-mike", "Mike", "+61456224867", trusted=1)
    await _seed_contact(ctx.db, "cid-david", "David", "+61401203022")
    await groups.upsert_member(gid, "cid-david", display_name="David",
                               now_iso=NOW)

    g_owner = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="a",
        creator_contact_id="cid-mike")
    g_member = await goal_service.create_goal(
        ctx, conversation_id=DM_SESSION, objective="b",
        creator_contact_id="cid-david")

    trusted, contact_id = await goal_rooms.room_creator_principal(
        ctx, goal_rooms.room_session_key(g_owner["id"]))
    owner_tools = {t.name: t for t in make_group_lookup_tools(
        ctx, is_trusted=trusted, contact_id=contact_id)}
    assert "Group: AI doom" in await owner_tools["group_participants"].handler(
        "AI doom")

    trusted, contact_id = await goal_rooms.room_creator_principal(
        ctx, goal_rooms.room_session_key(g_member["id"]))
    member_tools = {t.name: t for t in make_group_lookup_tools(
        ctx, is_trusted=trusted, contact_id=contact_id)}
    assert "Group: AI doom" in await member_tools[
        "group_participants"].handler("AI doom")       # David's group: yes
    assert "Not allowed" in await member_tools[
        "group_participants"].handler("Bob cast")      # other groups: no
