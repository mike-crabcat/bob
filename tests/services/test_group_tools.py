"""group_participants — cross-session roster lookup.

The session-scoped `participants` tool only works inside a group session;
this suite covers its any-conversation counterpart (2026-09-25): trusted
dispatches may query any group, untrusted only groups their contact
belongs to — the same scoping as the session history tools.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.repositories.groups import GroupRepository
from server.services.group_tools import (
    make_group_tools, make_group_lookup_tools,
)

GROUP_JID = "120363401238199025@g.us"
OTHER_JID = "120363499999999999@g.us"
NOW = datetime.now(timezone.utc).isoformat()


async def _seed(db) -> tuple[str, str, str]:
    """Group with three members; returns (group_id, member_cid, outsider_cid)."""
    groups = GroupRepository(db)
    gid = await groups.upsert_group(
        GROUP_JID, name="AI doom", description="", member_count=3, now_iso=NOW)
    other = await groups.upsert_group(
        OTHER_JID, name="Secret club", description="", member_count=1, now_iso=NOW)

    cids: list[str] = []
    for name, phone, trusted in (
        ("Mike Cleaver", "+61456224867", 1),
        ("Chris", "+61424616977", 0),
        ("Outsider", "+61400000000", 0),
    ):
        cid = f"cid-{name.lower()}"
        await db.execute(
            "INSERT INTO contacts (id, name, phone_number, is_default, "
            "is_trusted, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
            (cid, name, phone, trusted, NOW, NOW))
        cids.append(cid)

    await groups.upsert_member(gid, cids[0], display_name="Mike",
                               is_super_admin=1, now_iso=NOW)
    await groups.upsert_member(gid, cids[1], display_name="Chris", now_iso=NOW)
    await groups.upsert_member(other, cids[2], display_name="Outsider", now_iso=NOW)
    # upsert_member's fresh-INSERT path drops admin flags (only the
    # conflict-update writes them) — set the super admin directly
    await db.execute(
        "UPDATE whatsappgroup_members SET is_super_admin = 1 WHERE contact_id = ?",
        (cids[0],))
    await groups.refresh_member_count(gid, now_iso=NOW)
    return gid, cids[1], cids[2]


def _lookup(tools):
    return {t.name: t for t in tools}["group_participants"].handler


@pytest.mark.asyncio
async def test_trusted_lookup_by_name(ctx):
    _, _, _ = await _seed(ctx.db)
    handler = _lookup(make_group_lookup_tools(ctx, is_trusted=True))
    out = await handler("AI doom")
    assert out.startswith("Group: AI doom (2 members)")
    assert "Mike (super admin, trusted) — +61456224867" in out
    assert "Chris — +61424616977" in out


@pytest.mark.asyncio
async def test_lookup_by_jid_and_digits(ctx):
    await _seed(ctx.db)
    handler = _lookup(make_group_lookup_tools(ctx, is_trusted=True))
    assert (await handler(GROUP_JID)).startswith("Group: AI doom")
    assert (await handler(GROUP_JID.split("@")[0])).startswith("Group: AI doom")


@pytest.mark.asyncio
async def test_untrusted_denied_for_non_member_group(ctx):
    _, _, outsider_cid = await _seed(ctx.db)
    handler = _lookup(make_group_lookup_tools(
        ctx, is_trusted=False, contact_id=outsider_cid))
    out = await handler("AI doom")
    assert out.startswith("Not allowed:")


@pytest.mark.asyncio
async def test_untrusted_allowed_for_own_group(ctx):
    _, chris_cid, _ = await _seed(ctx.db)
    handler = _lookup(make_group_lookup_tools(
        ctx, is_trusted=False, contact_id=chris_cid))
    out = await handler("doom")  # substring match
    assert out.startswith("Group: AI doom")
    assert "Chris" in out


@pytest.mark.asyncio
async def test_ambiguous_name_disambiguates(ctx):
    groups = GroupRepository(ctx.db)
    await groups.upsert_group("111@g.us", name="Bob cast", description="",
                              member_count=0, now_iso=NOW)
    await groups.upsert_group("222@g.us", name="Bob cast 2", description="",
                              member_count=0, now_iso=NOW)
    handler = _lookup(make_group_lookup_tools(ctx, is_trusted=True))
    out = await handler("cast")  # substring, no exact match → ambiguous
    assert out.startswith("Multiple groups match")
    assert "111@g.us" in out and "222@g.us" in out


@pytest.mark.asyncio
async def test_no_match_and_empty(ctx):
    handler = _lookup(make_group_lookup_tools(ctx, is_trusted=True))
    assert (await handler("nope")).startswith("No group matching")
    assert (await handler("  ")).startswith("Error:")


@pytest.mark.asyncio
async def test_session_scoped_participants_unchanged(ctx):
    """Refactor regression: the group-session tool still renders via the
    shared helper and reports outside group sessions."""
    tools = {t.name: t for t in make_group_tools(ctx, session_key="agent:x:dm:1")}
    assert await tools["participants"].handler() == "Not in a group session."
