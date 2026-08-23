"""Tests for session tools — access scoping and recent-message reads.

Regression context (2026-08-17): a routine run in a WhatsApp group session
resolved to is_trusted=False with contact_id=None (group routes carry no
contact), and find_session's untrusted filter then hid every session — the
routine could not see even the conversation it was posting into, so it worked
from stale remembered confirmations.
"""

from __future__ import annotations

import json

import pytest

from bob_server.services.session_tools import make_session_tools
from bob_server.services.session_service import SessionService

GROUP_KEY = "agent:main:whatsapp:group:111"
OTHER_GROUP_KEY = "agent:main:whatsapp:group:222"
DM_KEY = "agent:main:whatsapp:dm:61400000001"

NOW = "2026-08-17T09:00:00+00:00"


@pytest.fixture
async def seeded(ctx):
    """Two group sessions, one DM session, and messages in the first group."""
    await ctx.db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, created_at, updated_at) "
        "VALUES ('g1', '111@g.us', 'Leeming Boys', ?, ?)", (NOW, NOW))
    await ctx.db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, created_at, updated_at) "
        "VALUES ('g2', '222@g.us', 'AI Doom', ?, ?)", (NOW, NOW))
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c1', 'Trevor', '+61431939512', ?, ?)", (NOW, NOW))
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c2', 'Mike', '+61400000001', ?, ?)", (NOW, NOW))

    for key, chat_id in ((GROUP_KEY, "111@g.us"), (OTHER_GROUP_KEY, "222@g.us")):
        await ctx.db.execute(
            "INSERT INTO session_routes (id, channel, session_key, kind, chat_id, created_at, updated_at) "
            "VALUES (?, 'whatsapp', ?, 'group', ?, ?, ?)",
            (f"sr-{chat_id}", key, chat_id, NOW, NOW))
    await ctx.db.execute(
        "INSERT INTO session_routes (id, channel, session_key, kind, contact_id, created_at, updated_at) "
        "VALUES ('sr-dm', 'whatsapp', ?, 'dm', 'c2', ?, ?)", (DM_KEY, NOW, NOW))

    # find_session reads bindings (Increment 4) — seed the mirrored rows the
    # route service dual-write would produce.
    from bob_server.repositories.conversations import ConversationRepository
    repo = ConversationRepository(ctx.db)
    await repo.ensure(GROUP_KEY, address="111@g.us", endpoint_kind="group")
    await repo.ensure(OTHER_GROUP_KEY, address="222@g.us", endpoint_kind="group")
    await repo.ensure(DM_KEY, address="+61400000001", endpoint_kind="dm")
    await ctx.db.execute(
        "UPDATE bindings SET contact_id = 'c2' WHERE session_key = ?", (DM_KEY,))

    session_svc = SessionService(ctx)
    await session_svc.add_message(GROUP_KEY, "user", "Lunch Sunday?", channel="whatsapp")
    await session_svc.add_message(
        GROUP_KEY, "user", "I'm in and I'm bringing my kids",
        channel="whatsapp", sender_id="c1")
    await session_svc.add_message(GROUP_KEY, "assistant", "Noted.", channel="whatsapp")
    return ctx


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


# ---------------------------------------------------------------------------
# find_session — accessible-set scoping
# ---------------------------------------------------------------------------


async def test_untrusted_group_context_finds_own_session(seeded):
    # The production bug: untrusted + no contact previously saw nothing.
    tools = make_session_tools(seeded, is_trusted=False, contact_id=None, session_key=GROUP_KEY)
    result = json.loads(await _tool(tools, "find_session").handler(query="Leeming Boys"))
    keys = [m["session_key"] for m in result["matches"]]
    assert GROUP_KEY in keys


async def test_untrusted_group_context_cannot_find_other_sessions(seeded):
    tools = make_session_tools(seeded, is_trusted=False, contact_id=None, session_key=GROUP_KEY)
    result = json.loads(await _tool(tools, "find_session").handler(query="AI Doom"))
    assert result["matches"] == []


async def test_untrusted_contact_finds_participating_sessions(seeded):
    # Trevor participates in the Leeming Boys group but is dispatching from his DM.
    await seeded.db.execute(
        "INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at) "
        "VALUES (?, '+61431939512', 'Trevor', 'c1', 0, ?)", (GROUP_KEY, NOW))
    tools = make_session_tools(seeded, is_trusted=False, contact_id="c1", session_key=DM_KEY)
    result = json.loads(await _tool(tools, "find_session").handler(query="Leeming Boys"))
    assert GROUP_KEY in [m["session_key"] for m in result["matches"]]
    result = json.loads(await _tool(tools, "find_session").handler(query="AI Doom"))
    assert result["matches"] == []


async def test_trusted_context_finds_all_sessions(seeded):
    tools = make_session_tools(seeded, is_trusted=True, session_key=GROUP_KEY)
    result = json.loads(await _tool(tools, "find_session").handler(query="AI Doom"))
    assert OTHER_GROUP_KEY in [m["session_key"] for m in result["matches"]]


# ---------------------------------------------------------------------------
# get_session_messages — reads with sender attribution
# ---------------------------------------------------------------------------


async def test_get_session_messages_defaults_to_current_session(seeded):
    tools = make_session_tools(seeded, is_trusted=False, contact_id=None, session_key=GROUP_KEY)
    result = json.loads(await _tool(tools, "get_session_messages").handler())
    assert result["session_key"] == GROUP_KEY
    contents = [m["content"] for m in result["messages"]]
    # Oldest first, most recent last
    assert contents == ["Lunch Sunday?", "I'm in and I'm bringing my kids", "Noted."]


async def test_get_session_messages_resolves_sender_names(seeded):
    tools = make_session_tools(seeded, is_trusted=False, contact_id=None, session_key=GROUP_KEY)
    result = json.loads(await _tool(tools, "get_session_messages").handler())
    by_content = {m["content"]: m for m in result["messages"]}
    assert by_content["I'm in and I'm bringing my kids"]["sender"] == "Trevor"
    assert by_content["Lunch Sunday?"]["sender"] is None


async def test_get_session_messages_returns_most_recent_not_oldest(seeded):
    session_svc = SessionService(seeded)
    for i in range(8):
        await session_svc.add_message(GROUP_KEY, "user", f"filler {i}", channel="whatsapp")
    tools = make_session_tools(seeded, is_trusted=False, contact_id=None, session_key=GROUP_KEY)
    result = json.loads(await _tool(tools, "get_session_messages").handler(limit=5))
    contents = [m["content"] for m in result["messages"]]
    assert len(contents) == 5
    assert contents[-1] == "filler 7"
    assert "Lunch Sunday?" not in contents  # pre-fill history dropped, not kept


async def test_get_session_messages_rejects_inaccessible_session(seeded):
    tools = make_session_tools(seeded, is_trusted=False, contact_id=None, session_key=GROUP_KEY)
    result = json.loads(
        await _tool(tools, "get_session_messages").handler(session_key=OTHER_GROUP_KEY))
    assert "error" in result


async def test_trusted_get_session_messages_reads_any_session(seeded):
    tools = make_session_tools(seeded, is_trusted=True, session_key=DM_KEY)
    result = json.loads(
        await _tool(tools, "get_session_messages").handler(session_key=GROUP_KEY))
    assert len(result["messages"]) == 3


async def test_build_common_tools_wires_current_session(seeded):
    from bob_server.services.tool_registry import build_common_tools

    tools = build_common_tools(seeded, session_key=GROUP_KEY, is_trusted=False, include_routines=False)
    read = _tool(tools, "get_session_messages")
    result = json.loads(await read.handler())
    assert result["session_key"] == GROUP_KEY
