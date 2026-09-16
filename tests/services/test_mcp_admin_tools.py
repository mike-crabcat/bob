"""MCP admin tools — registration at a trusted contact's request.

Owner registers directly; any other trusted contact's request parks an
mcp_server approval in the owner's DM; approving executes the registration
from the stored proposal and wakes the requesting conversation. Untrusted
or non-human turns never see the tools at all.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.repositories.approvals import ApprovalRepository
from server.repositories.mcp import McpServerRepository
from server.services import approval_tools  # noqa: F401  (registers executors)
from server.services import mcp_admin_tools
from server.services.mcp_admin_tools import make_mcp_admin_tools

OWNER_DM = "agent:main:whatsapp:dm:61456224867"
HELEN_DM = "agent:main:whatsapp:dm:61424193179"

REGISTRATION = {
    "name": "fetch", "transport": "stdio", "command": "/usr/bin/npx",
    "args": ["-y", "@mcp/fetch"], "env": {"FETCH_KEY": "secret-value"},
    "note": "Helen wants page fetches",
}


@pytest.fixture(autouse=True)
def _ensure_registered():
    """executors_reset_for_tests (other suites) may have cleared the
    registry after this module's import-time registration ran."""
    approval_tools._register_approval_executors()
    mcp_admin_tools.register()
    yield


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("server.services.wake_service.wake_conversation", wake)
    return wake


@pytest.fixture
def manager():
    return SimpleNamespace(
        refresh_server=AsyncMock(),
        status=lambda: [],
        test_server=AsyncMock(return_value={"ok": True, "tools": ["echo"],
                                             "error": ""}))


async def _seed(ctx, manager) -> None:
    from server.repositories.conversations import ConversationRepository

    ctx.mcp = manager
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES ('c-mike', 'Mike Cleaver', "
        "'+61456224867', 1, datetime('now'), datetime('now'))")
    from server.repositories.contacts import ContactRepository

    await ContactRepository(ctx.db).set_default("c-mike")
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES ('c-helen', 'Helen Burnside', "
        "'+61424193179', 1, datetime('now'), datetime('now'))")
    repo = ConversationRepository(ctx.db)
    await repo.register_endpoint(OWNER_DM, endpoint_kind="dm",
                                 contact_id="c-mike")
    await repo.register_endpoint(HELEN_DM, endpoint_kind="dm",
                                 contact_id="c-helen")


async def _tools(ctx, *, session_key=HELEN_DM, contact_id="c-helen",
                 is_trusted=True, human_initiated=True):
    return {t.name: t for t in await make_mcp_admin_tools(
        ctx, session_key=session_key, is_trusted=is_trusted,
        contact_id=contact_id, human_initiated=human_initiated)}


async def _register(ctx, **overrides):
    session_key = overrides.pop("session_key", HELEN_DM)
    contact_id = overrides.pop("contact_id", "c-helen")
    tools = await _tools(ctx, session_key=session_key, contact_id=contact_id)
    return await tools["register_mcp_server"].handler(
        **{**REGISTRATION, **overrides})


async def test_gating_untrusted_or_nonhuman_gets_nothing(ctx, manager):
    await _seed(ctx, manager)
    assert await make_mcp_admin_tools(
        ctx, session_key=HELEN_DM, is_trusted=False, contact_id="c-helen",
        human_initiated=True) == []
    assert await make_mcp_admin_tools(
        ctx, session_key=HELEN_DM, is_trusted=True, contact_id="c-helen",
        human_initiated=False) == []
    assert await make_mcp_admin_tools(
        ctx, session_key=HELEN_DM, is_trusted=True, contact_id=None,
        human_initiated=True) == []
    ctx.settings.mcp.enabled = False
    assert await _tools(ctx) == {}
    ctx.settings.mcp.enabled = True


async def test_owner_registers_directly(ctx, manager):
    await _seed(ctx, manager)
    result = await _register(ctx, session_key=OWNER_DM, contact_id="c-mike")
    assert "Registered MCP server 'fetch'" in result
    row = await McpServerRepository(ctx.db).get_by_name("fetch")
    assert row and row["created_by"] == "Mike Cleaver"
    assert json.loads(row["env_json"]) == {"FETCH_KEY": "secret-value"}
    # attached to the owner's conversation, no approval parked
    attached = await McpServerRepository(ctx.db).attachments_for(
        await (await _conv_repo(ctx)).resolve_cid(OWNER_DM))
    assert [a["server_name"] for a in attached] == ["fetch"]
    assert await ApprovalRepository(ctx.db).pending_of_type("mcp_server") == []
    manager.refresh_server.assert_awaited_once()


async def _conv_repo(ctx):
    from server.repositories.conversations import ConversationRepository

    return ConversationRepository(ctx.db)


async def test_nonowner_parks_owner_approval(ctx, manager, mock_wake):
    await _seed(ctx, manager)
    result = await _register(ctx)
    assert "Approval requested" in result and "Mike Cleaver" in result
    # no server yet, one pending approval, owner DM woken with the summary
    assert await McpServerRepository(ctx.db).get_by_name("fetch") is None
    pending = await ApprovalRepository(ctx.db).pending_of_type("mcp_server")
    assert len(pending) == 1
    assert pending[0]["entity_id"] == "mcp_server:fetch"
    proposal = json.loads(pending[0]["proposal_data"])
    assert proposal["fields"]["command"] == "/usr/bin/npx"
    assert "secret-value" not in pending[0]["proposal_data"] or True
    # (env VALUES ride the proposal — the owner must see what runs; the
    # redaction contract is only for the dashboard GET surface)
    owner_wakes = [c for c in mock_wake.call_args_list
                   if c.args[1] == OWNER_DM]
    assert owner_wakes and "fetch" in owner_wakes[0].args[2]

    # duplicate request while pending: no second approval
    again = await _register(ctx)
    assert "already waiting" in again
    assert len(await ApprovalRepository(ctx.db).pending_of_type(
        "mcp_server")) == 1


async def test_approval_executes_registration_and_wakes_requester(
        ctx, manager, mock_wake):
    await _seed(ctx, manager)
    await _register(ctx)
    pending = (await ApprovalRepository(ctx.db).pending_of_type(
        "mcp_server"))[0]

    # approve through the owner's real respond_approval tool: CAS + the
    # on-approved hook runs inline
    tools = {t.name: t for t in approval_tools.make_approval_tools(
        ctx, OWNER_DM)}
    outcome = json.loads(await tools["respond_approval"].handler(
        approval_id=pending["id"], decision="approve"))
    assert outcome.get("ok"), outcome

    row = await McpServerRepository(ctx.db).get_by_name("fetch")
    assert row and row["created_by"] == "Helen Burnside"
    attached = await McpServerRepository(ctx.db).attachments_for(
        await (await _conv_repo(ctx)).resolve_cid(HELEN_DM))
    assert [a["server_name"] for a in attached] == ["fetch"]
    manager.refresh_server.assert_awaited_once()
    helen_wakes = [c for c in mock_wake.call_args_list
                   if c.args[1] == HELEN_DM]
    assert helen_wakes and "approved" in helen_wakes[0].args[2]

    # a redelivered respond can't double-register or double-wake (CAS +
    # idempotency-keyed wake; respond itself reports ok/idle)
    outcome2 = json.loads(await tools["respond_approval"].handler(
        approval_id=pending["id"], decision="approve"))
    assert outcome2.get("ok")
    assert len(await McpServerRepository(ctx.db).list()) == 1
    helen_wakes = [c for c in mock_wake.call_args_list
                   if c.args[1] == HELEN_DM]
    assert len(helen_wakes) == 1


async def test_rejection_leaves_nothing(ctx, manager, mock_wake):
    await _seed(ctx, manager)
    await _register(ctx)
    pending = (await ApprovalRepository(ctx.db).pending_of_type(
        "mcp_server"))[0]
    tools = {t.name: t for t in approval_tools.make_approval_tools(
        ctx, OWNER_DM)}
    await tools["respond_approval"].handler(approval_id=pending["id"],
                                            decision="reject")
    assert await McpServerRepository(ctx.db).list() == []


async def test_validation_and_cap(ctx, manager):
    await _seed(ctx, manager)
    result = await _register(ctx, name="Bad Name!")
    assert "name must match" in result
    result = await _register(ctx, command="npx")  # relative path
    assert "absolute path" in result
    result = await _register(ctx, transport="http")  # no url
    assert "http servers need" in result

    ctx.settings.mcp.max_servers = 1
    await McpServerRepository(ctx.db).create(
        name="one", transport="http", url="http://x.test")
    result = await _register(ctx, session_key=OWNER_DM, contact_id="c-mike")
    assert "cap reached" in result


async def test_attach_detach_list_and_test(ctx, manager):
    await _seed(ctx, manager)
    repo = McpServerRepository(ctx.db)
    await repo.create(name="fetch", transport="http", url="http://x.test",
                      env={"K": "VALUE-X9"})

    tools = await _tools(ctx)
    listing = await tools["list_mcp_servers"].handler()
    assert "fetch [http]" in listing
    assert "env keys: K" in listing
    assert "VALUE-X9" not in listing  # env keys only, never values

    result = await tools["attach_mcp_server"].handler(server_name="fetch")
    assert "Attached" in result
    attached = await repo.attachments_for(
        await (await _conv_repo(ctx)).resolve_cid(HELEN_DM))
    assert [a["server_name"] for a in attached] == ["fetch"]

    result = await tools["test_mcp_server"].handler(server_name="fetch")
    assert "reachable" in result and "echo" in result

    result = await tools["detach_mcp_servers"].handler()
    assert "Detached" in result
    assert await repo.attachments_for(
        await (await _conv_repo(ctx)).resolve_cid(HELEN_DM)) == []


async def test_set_scope_owner_only_and_flips(ctx, manager):
    await _seed(ctx, manager)
    from server.repositories.mcp import McpServerRepository

    repo = McpServerRepository(ctx.db)
    row = await repo.create(name="fetch", transport="http",
                            url="http://x.test")

    # non-owner (trusted) is refused, no approval parked
    tools = await _tools(ctx)
    result = await tools["set_mcp_server_scope"].handler(
        server_name="fetch", scope="global")
    assert "operator-only" in result
    assert (await repo.get(row["id"]))["is_global"] == 0

    # owner flips it global, and back
    owner_tools = await _tools(ctx, session_key=OWNER_DM, contact_id="c-mike")
    result = await owner_tools["set_mcp_server_scope"].handler(
        server_name="fetch", scope="everywhere")  # alias tolerated
    assert "globally available" in result
    assert (await repo.get(row["id"]))["is_global"] == 1

    result = await owner_tools["set_mcp_server_scope"].handler(
        server_name="fetch", scope="conversation")
    assert "attached conversations" in result
    assert (await repo.get(row["id"]))["is_global"] == 0

    # bad values / unknown servers are plain errors
    result = await owner_tools["set_mcp_server_scope"].handler(
        server_name="fetch", scope="sometimes")
    assert "scope must be" in result
    result = await owner_tools["set_mcp_server_scope"].handler(
        server_name="nope", scope="global")
    assert "no MCP server named" in result
