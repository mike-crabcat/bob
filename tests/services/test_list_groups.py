"""list_groups — one-call group census (2026-10-09: Bob probed ~60 calls
with find_session('a'/'e'/'o') to find 'groups active this week')."""

from server.services.group_tools import make_group_lookup_tools


async def test_list_groups_empty_and_bad_since(ctx):
    tools = {t.name: t for t in make_group_lookup_tools(ctx, is_trusted=True)}
    assert "list_groups" in tools
    assert await tools["list_groups"].handler() == "Bob is in no groups."
    assert (await tools["list_groups"].handler(active_since="last tuesday")).startswith("Error")
    assert await tools["list_groups"].handler(active_since="7d") == "No groups match."
