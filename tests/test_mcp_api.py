"""Dashboard MCP API tests: token gate, secret redaction on GETs,
"***"-preserving PUTs, CRUD + per-conversation attachments."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db",
        **overrides,
    )


def _client(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    return settings, app, client


def _auth(settings: Settings) -> dict:
    return {"X-Dashboard-Secret": settings.resolved_api_secret}


def test_post_denied_without_token(tmp_path: Path) -> None:
    settings, app, client = _client(tmp_path)
    with client:
        response = client.post(
            "/dashboard/api/mcp/servers",
            json={"name": "x", "transport": "http", "url": "http://x.test"})
        assert response.status_code == 401


async def test_create_get_redacts_and_put_preserves_secrets(tmp_path: Path) -> None:
    settings, app, client = _client(tmp_path)
    with client:
        headers = _auth(settings)
        created = client.post(
            "/dashboard/api/mcp/servers",
            json={"name": "fetcher", "transport": "http",
                  "url": "https://example.test/mcp",
                  "headers": {"Authorization": "Bearer real-token"}},
            headers=headers)
        assert created.status_code == 200, created.text
        server = created.json()["server"]
        # the CREATE response is redacted too
        assert server["headers_json"] == {"Authorization": "***"}

        listed = client.get("/dashboard/api/mcp/servers", headers=headers)
        row = listed.json()["servers"][0]
        assert row["headers_json"] == {"Authorization": "***"}

        # a GET without a token never reaches data (handler-level auth)
        assert client.get("/dashboard/api/mcp/servers").json() == {
            "error": "unauthorized"}

        # round-tripping the redacted value through PUT keeps the secret.
        # (sqlite3 on the file DB: the app runs in TestClient's portal loop,
        # so app.state.db can't be awaited from this test's loop.)
        import sqlite3
        updated = client.put(
            f"/dashboard/api/mcp/servers/{server['id']}",
            json={"headers": {"Authorization": "***", "X-New": "v"},
                  "env": {"FRESH_KEY": "fresh"}},
            headers=headers)
        assert updated.status_code == 200, updated.text

        conn = sqlite3.connect(settings.db_path)
        try:
            stored_headers, stored_env = conn.execute(
                "SELECT headers_json, env_json FROM mcp_servers WHERE id = ?",
                (server["id"],)).fetchone()
        finally:
            conn.close()
        assert json.loads(stored_headers) == {
            "Authorization": "Bearer real-token", "X-New": "v"}
        assert json.loads(stored_env) == {"FRESH_KEY": "fresh"}


def test_validation_rejects_bad_names_and_relative_commands(tmp_path: Path):
    settings, app, client = _client(tmp_path)
    with client:
        headers = _auth(settings)
        for body, expect in [
            ({"name": "Bad Name", "transport": "http", "url": "http://x"},
             "name must match"),
            ({"name": "ok", "transport": "grpc", "url": "http://x"},
             "transport must be"),
            ({"name": "ok", "transport": "stdio", "command": "npx"},
             "absolute path"),
            ({"name": "ok", "transport": "stdio"}, "stdio servers need"),
            ({"name": "ok", "transport": "http"}, "http servers need"),
            ({"name": "ok", "transport": "http", "url": "ftp://x"},
             "url must start with"),
        ]:
            response = client.post("/dashboard/api/mcp/servers",
                                   json=body, headers=headers)
            assert expect in response.json()["error"], body

        # duplicate name rejected
        body = {"name": "dup", "transport": "http", "url": "http://x.test"}
        assert client.post("/dashboard/api/mcp/servers", json=body,
                           headers=headers).status_code == 200
        again = client.post("/dashboard/api/mcp/servers", json=body,
                            headers=headers)
        assert "already exists" in again.json()["error"]


def test_attachments_flow_and_delete_cascades(tmp_path: Path):
    settings, app, client = _client(tmp_path)
    with client:
        headers = _auth(settings)
        a = client.post("/dashboard/api/mcp/servers", headers=headers,
                        json={"name": "aaa", "transport": "http",
                              "url": "http://a.test"}).json()["server"]
        b = client.post("/dashboard/api/mcp/servers", headers=headers,
                        json={"name": "bbb", "transport": "http",
                              "url": "http://b.test"}).json()["server"]

        cid = "agent:main:whatsapp:dm:+61400000000"
        put = client.put(f"/dashboard/api/conversations/{cid}/mcp",
                         json={"server_ids": [a["id"], b["id"]]},
                         headers=headers)
        assert put.status_code == 200, put.text

        got = client.get(f"/dashboard/api/conversations/{cid}/mcp",
                         headers=headers).json()
        assert [s["name"] for s in got["attached"]] == ["aaa", "bbb"]
        assert got["available"] == []

        # replace semantics + unknown id rejection
        put = client.put(f"/dashboard/api/conversations/{cid}/mcp",
                         json={"server_ids": [a["id"]]}, headers=headers)
        got = client.get(f"/dashboard/api/conversations/{cid}/mcp",
                         headers=headers).json()
        assert [s["name"] for s in got["attached"]] == ["aaa"]
        assert [s["name"] for s in got["available"]] == ["bbb"]

        bad = client.put(f"/dashboard/api/conversations/{cid}/mcp",
                         json={"server_ids": ["nope"]}, headers=headers)
        assert "unknown server ids" in bad.json()["error"]

        deleted = client.delete(f"/dashboard/api/mcp/servers/{a['id']}",
                                headers=headers)
        assert deleted.json() == {"ok": True}
        got = client.get(f"/dashboard/api/conversations/{cid}/mcp",
                         headers=headers).json()
        assert got["attached"] == []
        assert client.delete(f"/dashboard/api/mcp/servers/{a['id']}",
                             headers=headers).json() == {"error": "not found"}


async def test_enabled_toggle_and_test_endpoint(tmp_path: Path):
    settings, app, client = _client(tmp_path)
    with client:
        headers = _auth(settings)
        server = client.post(
            "/dashboard/api/mcp/servers", headers=headers,
            json={"name": "dead", "transport": "http",
                  "url": "http://127.0.0.1:1/mcp"}).json()["server"]

        # test hits a fresh connection: port 1 refuses instantly
        result = client.post(f"/dashboard/api/mcp/servers/{server['id']}/test",
                             headers=headers).json()
        assert result["ok"] is False and result["error"]

        toggle = client.post(f"/dashboard/api/mcp/servers/{server['id']}/enabled",
                             json={"enabled": False}, headers=headers)
        assert toggle.json() == {"ok": True, "enabled": False}
        assert client.post(
            f"/dashboard/api/mcp/servers/{server['id']}/enabled",
            json={"nope": 1}, headers=headers).json()["error"]

        # scoping excludes disabled servers even when attached. The app runs
        # in TestClient's portal loop — read the file DB with a separate
        # sqlite3 connection instead of app.state.db (cross-loop aiosqlite
        # use is undefined).
        import sqlite3

        def scoped_names() -> list[str]:
            conn = sqlite3.connect(settings.db_path)
            try:
                rows = conn.execute(
                    "SELECT name FROM mcp_servers WHERE enabled = 1 AND "
                    "(is_global = 1 OR id IN (SELECT mcp_server_id FROM "
                    "conversation_mcp_attachments WHERE conversation_id = ?))",
                    ("conv-1",)).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()

        cid = "conv-1"
        client.put(f"/dashboard/api/conversations/{cid}/mcp",
                   json={"server_ids": [server["id"]]}, headers=headers)
        assert scoped_names() == []
        client.post(f"/dashboard/api/mcp/servers/{server['id']}/enabled",
                    json={"enabled": True}, headers=headers)
        assert scoped_names() == ["dead"]
