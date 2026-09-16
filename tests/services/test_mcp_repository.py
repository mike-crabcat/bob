"""McpServerRepository: DDL, CRUD, scoping, attachment replace semantics."""

from __future__ import annotations

import json

from server.repositories.mcp import McpServerRepository


async def _seed_conversation(db) -> str:
    await db.execute(
        "INSERT INTO conversations (id, kind, created_at, updated_at) "
        "VALUES ('cid-1', 'dm', '2026-09-14T00:00:00+00:00', "
        "'2026-09-14T00:00:00+00:00')")
    return "cid-1"


async def test_tables_exist(db):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('mcp_servers', 'conversation_mcp_attachments')")
    names = {r["name"] for r in rows}
    assert names == {"mcp_servers", "conversation_mcp_attachments"}


async def test_create_round_trip_and_json_columns(db):
    repo = McpServerRepository(db)
    row = await repo.create(
        name="fetch", transport="stdio", command="/usr/bin/npx",
        args=["-y", "@mcp/fetch"], env={"API_KEY": "s3cret"},
        is_global=True, timeout_seconds=90, note="test server")
    assert row["name"] == "fetch"
    assert row["args_json"] == json.dumps(["-y", "@mcp/fetch"])
    assert json.loads(row["env_json"]) == {"API_KEY": "s3cret"}
    assert row["enabled"] == 1 and row["is_global"] == 1
    assert row["timeout_seconds"] == 90
    fetched = await repo.get_by_name("fetch")
    assert fetched and fetched["id"] == row["id"]


async def test_servers_for_conversation_scoping(db):
    cid = await _seed_conversation(db)
    repo = McpServerRepository(db)
    glob = await repo.create(name="global-one", transport="http",
                             url="https://example.test/mcp", is_global=True)
    attached = await repo.create(name="attached-one", transport="stdio",
                                 command="/bin/true")
    await repo.create(name="unattached", transport="stdio", command="/bin/true")
    disabled = await repo.create(name="disabled-glob", transport="stdio",
                                 command="/bin/true", is_global=True,
                                 enabled=False)

    rows = await repo.servers_for_conversation(cid)
    names = {r["name"] for r in rows}
    assert names == {"global-one"}  # unattached + disabled excluded
    await repo.set_attachments(cid, [attached["id"]])
    rows = await repo.servers_for_conversation(cid)
    assert {r["name"] for r in rows} == {"global-one", "attached-one"}
    await repo.set_enabled(disabled["id"], True)
    rows = await repo.servers_for_conversation(cid)
    assert {r["name"] for r in rows} == {
        "global-one", "attached-one", "disabled-glob"}
    assert glob["id"] != attached["id"]


async def test_set_attachments_replaces(db):
    cid = await _seed_conversation(db)
    repo = McpServerRepository(db)
    a = await repo.create(name="a", transport="stdio", command="/bin/true")
    b = await repo.create(name="b", transport="stdio", command="/bin/true")
    await repo.set_attachments(cid, [a["id"]], attached_by="mike")
    await repo.set_attachments(cid, [b["id"]])  # replace, not union
    att = await repo.attachments_for(cid)
    assert [r["server_name"] for r in att] == ["b"]
    assert att[0]["attached_by"] == ""

    # duplicate ids collapse on the PK
    await repo.set_attachments(cid, [a["id"], a["id"]])
    assert len(await repo.attachments_for(cid)) == 1


async def test_update_redacted_secrets_kept(db):
    repo = McpServerRepository(db)
    row = await repo.create(name="redact", transport="http",
                            url="https://example.test/mcp",
                            headers={"Authorization": "Bearer real-value"})
    updated = await repo.update(
        row["id"],
        headers={"Authorization": "***", "X-Extra": "new"},
        env={"FRESH": "also-new"})
    assert json.loads(updated["headers_json"]) == {
        "Authorization": "Bearer real-value", "X-Extra": "new"}
    assert json.loads(updated["env_json"]) == {"FRESH": "also-new"}

    # explicit non-redacted rewrite still works
    updated = await repo.update(row["id"], headers={"Authorization": "tok2"})
    assert json.loads(updated["headers_json"]) == {"Authorization": "tok2"}


async def test_update_timeout_null_and_fields(db):
    repo = McpServerRepository(db)
    row = await repo.create(name="timeouts", transport="stdio",
                            command="/bin/true", timeout_seconds=30)
    updated = await repo.update(row["id"], timeout_seconds=None)
    assert updated["timeout_seconds"] is None
    updated = await repo.update(row["id"], timeout_seconds=120, note="hi",
                                trusted_only=True)
    assert updated["timeout_seconds"] == 120
    assert updated["note"] == "hi" and updated["trusted_only"] == 1
    # unknown keys are ignored, no-op update returns row
    again = await repo.update(row["id"], bogus_column="x")
    assert again["id"] == row["id"]


async def test_delete_cascades_attachments(db):
    cid = await _seed_conversation(db)
    repo = McpServerRepository(db)
    a = await repo.create(name="gone", transport="stdio", command="/bin/true")
    keep = await repo.create(name="stay", transport="stdio", command="/bin/true")
    await repo.set_attachments(cid, [a["id"], keep["id"]])
    assert await repo.delete(a["id"]) is True
    assert await repo.get(a["id"]) is None
    att = await repo.attachments_for(cid)
    assert [r["server_name"] for r in att] == ["stay"]
    assert await repo.delete(a["id"]) is False  # second delete: no-op
    convs = await repo.conversations_for_server(keep["id"])
    assert [c["conversation_id"] for c in convs] == [cid]
