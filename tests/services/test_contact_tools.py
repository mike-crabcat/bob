"""Tests for the create_contact agent tool."""

from __future__ import annotations

import json

from bob_server.services.contact_tools import make_contact_tools


def _tool_map(ctx, *, trusted: bool):
    return {t.name: t for t in make_contact_tools(ctx, is_trusted=trusted)}


async def _call(tool, **kwargs) -> dict:
    return json.loads(await tool.handler(**kwargs))


async def test_untrusted_sessions_get_search_only(ctx):
    tools = _tool_map(ctx, trusted=False)
    assert "search_contacts" in tools
    assert "create_contact" not in tools


async def test_trusted_sessions_get_create_contact(ctx):
    tools = _tool_map(ctx, trusted=True)
    assert "create_contact" in tools


async def test_create_contact_normalizes_phone_and_sets_outbound_flags(ctx):
    tools = _tool_map(ctx, trusted=True)
    result = await _call(
        tools["create_contact"],
        name="JB Hi-Fi Osborne Park",
        phone_number="(08) 9244 5300",
    )
    assert result["created"] is True
    assert result["phone_number"] == "+61892445300"
    assert result["allow_inbound_dm"] is False
    assert result["is_trusted"] is False

    row = await ctx.db.fetch_one(
        "SELECT name, phone_number, is_trusted, allow_inbound_dm FROM contacts WHERE id = ?",
        (result["contact_id"],),
    )
    assert row["name"] == "JB Hi-Fi Osborne Park"
    assert row["phone_number"] == "+61892445300"
    assert row["is_trusted"] == 0
    assert row["allow_inbound_dm"] == 0


async def test_create_contact_dedupe_returns_existing_without_mutation(ctx):
    tools = _tool_map(ctx, trusted=True)
    first = await _call(tools["create_contact"], name="Shop", phone_number="08 9244 5300")

    # A human contact already exists for the same number, trusted + inbound allowed
    await ctx.db.execute(
        """UPDATE contacts SET name = 'The Real Human', is_trusted = 1,
                                 allow_inbound_dm = 1 WHERE id = ?""",
        (first["contact_id"],),
    )

    again = await _call(tools["create_contact"], name="Shop Again", phone_number="(08) 9244 5300")
    assert again["created"] is False
    assert again["contact_id"] == first["contact_id"]
    assert again["name"] == "The Real Human"
    assert again["allow_inbound_dm"] is True

    row = await ctx.db.fetch_one(
        "SELECT name, is_trusted, allow_inbound_dm FROM contacts WHERE id = ?",
        (first["contact_id"],),
    )
    assert row["name"] == "The Real Human"
    assert row["is_trusted"] == 1
    assert row["allow_inbound_dm"] == 1


async def test_create_contact_rejects_unparseable_phone(ctx):
    tools = _tool_map(ctx, trusted=True)
    result = await _call(tools["create_contact"], name="Ghost", phone_number="not-a-phone")
    assert "error" in result
    assert "could not parse" in result["error"]
    rows = await ctx.db.fetch_all("SELECT * FROM contacts WHERE name = 'Ghost'")
    assert rows == []


async def test_create_contact_rejects_blank_name(ctx):
    tools = _tool_map(ctx, trusted=True)
    result = await _call(tools["create_contact"], name="   ", phone_number="08 9244 5300")
    assert "error" in result
