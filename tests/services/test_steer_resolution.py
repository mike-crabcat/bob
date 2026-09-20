"""Steering target resolution — the membership gate and its one relaxation.

Membership binds the target for everyone (requester's own DM or groups);
since 2026-09-17 the owner additionally resolves against any group Bob
holds an active binding for (plan §5). This is the regression suite for
both sides of that line, plus the no-match error naming the requester —
"this user belongs to" read as *Bob* in the 2026-09-17 Nikesh-group
incident, and Bob told the owner he'd been removed from a group he posts
in daily.
"""

from __future__ import annotations

OWNER_ID = "c-mike"
BRAD_ID = "c-brad"
NIKESH_GROUP = {"jid": "120363430111642553@g.us", "name": "Bob and Nikesh"}
DOOM_GROUP = {"jid": "120363408889690088@g.us", "name": "AI Doom"}


def _key(jid: str) -> str:
    from server.services.steering import _digits
    return f"agent:main:whatsapp:group:{_digits(jid)}"


async def _seed(ctx) -> None:
    """Mike (owner, member of nothing), Brad (member of AI Doom), Bob active
    in Bob-and-Nikesh only; AI Doom carries a member row for Brad but no
    active binding (Bob left), Leeming Boys has members but no binding."""
    now = "2026-09-17 00:00:00"
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_default, is_trusted, "
        "created_at, updated_at) VALUES (?, 'Mike', '+61400000000', 1, 1, ?, ?)",
        (OWNER_ID, now, now))
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES (?, 'Brad', '+447523520214', 1, ?, ?)",
        (BRAD_ID, now, now))

    from server.repositories.conversations import ConversationRepository
    from server.repositories.groups import GroupRepository

    groups = GroupRepository(ctx.db)
    convs = ConversationRepository(ctx.db)
    for g in (NIKESH_GROUP, DOOM_GROUP,
              {"jid": "120363411111111111@g.us", "name": "Leeming Boys"}):
        await groups.ensure_group(g["jid"], g["name"], now)
        await convs.register_endpoint(
            _key(g["jid"]), endpoint_kind="group", address=g["jid"])
    # Bob left AI Doom and was never bound to Leeming Boys.
    await ctx.db.execute(
        "UPDATE bindings SET is_active = 0 WHERE session_key = ?",
        (_key(DOOM_GROUP["jid"]),))
    await ctx.db.execute(
        "DELETE FROM bindings WHERE session_key = ?",
        (_key("120363411111111111@g.us"),))

    # Brad participates in AI Doom and Leeming Boys; Mike in nothing.
    for jid in (DOOM_GROUP["jid"], "120363411111111111@g.us"):
        await ctx.db.execute(
            """INSERT INTO whatsappgroup_members
               (id, group_id, contact_id, joined_at, created_at, updated_at)
               SELECT ?, id, ?, ?, ?, ? FROM whatsappgroups
               WHERE whatsapp_jid = ?""",
            (f"gm-{jid[:8]}", BRAD_ID, now, now, now, jid))


async def _resolve(ctx, target: str, requester: str) -> dict:
    from server.services.steering import resolve_target
    return await resolve_target(ctx, target, requester_contact_id=requester)


async def test_owner_resolves_group_they_are_not_in(ctx):
    """The relaxation: Mike (owner) steers Bob-and-Nikesh by name and by raw
    id even though he's not a member — the 2026-09-17 incident, fixed."""
    await _seed(ctx)
    for target in (NIKESH_GROUP["name"], "120363430111642553"):
        result = await _resolve(ctx, target, OWNER_ID)
        assert result["ok"] is True, result
        assert result["target_key"] == _key(NIKESH_GROUP["jid"])
        assert result["target_kind"] == "group"
        assert result["target_label"] == NIKESH_GROUP["name"]


async def test_membership_still_binds_non_owners(ctx):
    """Brad (in AI Doom's member list, but Bob has no active binding there)
    cannot steer Bob-and-Nikesh: no match in his own groups, no fallback.
    The error names the requester, not 'this user'."""
    await _seed(ctx)
    result = await _resolve(ctx, "Bob and Nikesh", BRAD_ID)
    assert result["ok"] is False
    assert "that Brad belongs to" in result["error"]
    assert [c["name"] for c in result["candidates"]] == [
        "AI Doom", "Leeming Boys"]


async def test_matched_group_still_needs_active_binding(ctx):
    """Matching the requester's own membership isn't enough: a group Bob
    left (member row alive, binding inactive) is rejected — the gate that
    the 2026-09-17 incident misread as 'Bob was removed'."""
    await _seed(ctx)
    result = await _resolve(ctx, "AI Doom", BRAD_ID)
    assert result["ok"] is False
    assert result["error"] == "Bob is no longer active in AI Doom"


async def test_owner_no_match_lists_bobs_active_groups(ctx):
    """Owner asks for something that exists nowhere: the error says whose
    pools were searched and candidates are the groups Bob can actually post
    to (bound only — departed and never-joined groups excluded)."""
    await _seed(ctx)
    result = await _resolve(ctx, "Nonexistent", OWNER_ID)
    assert result["ok"] is False
    assert "that Mike or Bob belongs to" in result["error"]
    assert [c["name"] for c in result["candidates"]] == [NIKESH_GROUP["name"]]


async def test_group_registration_seeds_full_patience_default(ctx):
    """2026-09-20 operator default: every non-DM conversation gets the Tier 2
    relevance gate (patience_enabled + patience_relevance_gating) at
    registration; DMs seed nothing; an explicit policy is never overwritten."""
    from server.repositories.conversations import ConversationRepository

    convs = ConversationRepository(ctx.db)
    await convs.register_endpoint(
        "agent:main:whatsapp:group:120363430999999999",
        endpoint_kind="group", address="120363430999999999@g.us")
    assert await convs.get_policy(
        "agent:main:whatsapp:group:120363430999999999") == (
        ConversationRepository.GROUP_DEFAULT_POLICY)

    await convs.register_endpoint(
        "agent:main:whatsapp:dm:61400000001",
        endpoint_kind="dm", contact_id=None)
    assert await convs.get_policy("agent:main:whatsapp:dm:61400000001") == {}

    # deliberate opt-out survives re-registration (idempotent, no clobber)
    await convs.set_policy("agent:main:whatsapp:group:120363430999999999",
                           {"patience_enabled": False})
    await convs.register_endpoint(
        "agent:main:whatsapp:group:120363430999999999",
        endpoint_kind="group", address="120363430999999999@g.us")
    policy = await convs.get_policy("agent:main:whatsapp:group:120363430999999999")
    assert policy["patience_enabled"] is False
