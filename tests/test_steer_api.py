"""Dashboard wake-API tests: token gate, owner-steer wake shape (provenance
`steer`, requester attribution), and target resolution — phone → DM, group
name via the owner-wide pool, candidates on miss. The `bob steer` CLI is a
thin front over this endpoint (house pattern: no CLI tests of its own)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app

OWNER_ID = "c-mike"
GROUP_KEY = "agent:main:whatsapp:group:120363430111642553"
GROUP_JID = "120363430111642553@g.us"
BRAD_DM = "agent:main:whatsapp:dm:447523520214"
NOW = "2026-09-17T00:00:00"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db",
        **overrides,
    )


def _client(tmp_path: Path):
    settings = make_settings(tmp_path)
    return settings, TestClient(create_app(settings))


def _auth(settings: Settings) -> dict:
    return {"X-Dashboard-Secret": settings.resolved_api_secret}


def _seed(db_path: Path) -> None:
    """Owner + Brad contact, Brad's DM binding, Bob-and-Nikesh group with an
    active binding. Plain sqlite3 — the app runs in TestClient's portal loop
    so app.state.db can't be awaited from the test's loop (test_mcp_api
    pattern)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(f"""
        INSERT INTO contacts (id, name, phone_number, is_default, is_trusted,
                              created_at, updated_at)
        VALUES ('{OWNER_ID}', 'Mike', '+61456224867', 1, 1, '{NOW}', '{NOW}'),
               ('c-brad', 'Brad', '+447523520214', 0, 1, '{NOW}', '{NOW}');

        INSERT INTO conversations (id, kind, created_at, updated_at)
        VALUES ('{BRAD_DM}', 'dm', '{NOW}', '{NOW}'),
               ('{GROUP_KEY}', 'group', '{NOW}', '{NOW}');

        INSERT INTO bindings (session_key, conversation_id, channel, kind,
                              address, endpoint_kind, contact_id, is_active,
                              created_at)
        VALUES ('{BRAD_DM}', '{BRAD_DM}', 'whatsapp', 'thread',
                '447523520214', 'dm', 'c-brad', 1, '{NOW}'),
               ('{GROUP_KEY}', '{GROUP_KEY}', 'whatsapp', 'thread',
                '{GROUP_JID}', 'group', NULL, 1, '{NOW}');

        INSERT INTO whatsappgroups (id, whatsapp_jid, name, member_count,
                                    created_at, updated_at)
        VALUES ('g-1', '{GROUP_JID}', 'Bob and Nikesh', 3, '{NOW}', '{NOW}');
        """)
        conn.commit()
    finally:
        conn.close()


def _capture_wake(monkeypatch):
    """Patch wake_conversation where the endpoint imports it (function-local
    import → module-attribute patch holds)."""
    calls: list[dict] = []

    async def _fake_wake(ctx, session_key, content, *, call_category="wakeup",
                         metadata=None, provenance="wake_nudge"):
        calls.append({"session_key": session_key, "content": content,
                      "call_category": call_category,
                      "metadata": metadata or {}, "provenance": provenance})
        return True

    import server.services.wake_service as wake_svc
    monkeypatch.setattr(wake_svc, "wake_conversation", _fake_wake)
    return calls


def test_wake_denied_without_token(tmp_path: Path) -> None:
    settings, client = _client(tmp_path)
    with client:
        response = client.post(
            "/dashboard/api/conversations/wake",
            json={"target": BRAD_DM, "instruction": "x"})
        assert response.status_code == 401


def test_wake_by_phone_resolves_dm_and_steers(tmp_path, monkeypatch) -> None:
    settings, client = _client(tmp_path)
    calls = _capture_wake(monkeypatch)
    with client:
        _seed(settings.db_path)
        response = client.post(
            "/dashboard/api/conversations/wake",
            json={"target": "+447523520214",
                  "instruction": "tell Brad the fix landed"},
            headers=_auth(settings))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] and body["session_key"] == BRAD_DM
        assert body["dispatched"] is True

    assert len(calls) == 1
    wake = calls[0]
    assert wake["session_key"] == BRAD_DM
    assert wake["provenance"] == "steer"          # human-stimulus semantics
    assert wake["call_category"] == "steer"
    assert wake["content"].startswith("[Steering request — Mike,")
    assert "operator console" in wake["content"]  # honest about origin
    assert "tell Brad the fix landed" in wake["content"]
    assert wake["metadata"]["owner_direct"] is True
    assert wake["metadata"]["requester_contact_id"] == OWNER_ID


def test_wake_by_group_name_uses_owner_wide_pool(tmp_path, monkeypatch) -> None:
    """Mike isn't a member of Bob-and-Nikesh, but the operator steers into
    any group Bob holds an active binding for (2026-09-17 relaxation)."""
    settings, client = _client(tmp_path)
    calls = _capture_wake(monkeypatch)
    with client:
        _seed(settings.db_path)
        response = client.post(
            "/dashboard/api/conversations/wake",
            json={"target": "Bob and Nikesh", "instruction": "post the song"},
            headers=_auth(settings))
        assert response.status_code == 200, response.text
        assert response.json()["session_key"] == GROUP_KEY
        assert calls[0]["session_key"] == GROUP_KEY


def test_wake_unknown_target_lists_candidates(tmp_path, monkeypatch) -> None:
    settings, client = _client(tmp_path)
    _capture_wake(monkeypatch)
    with client:
        _seed(settings.db_path)
        response = client.post(
            "/dashboard/api/conversations/wake",
            json={"target": "Nonexistent", "instruction": "x"},
            headers=_auth(settings))
        assert response.status_code == 200
        body = response.json()
        assert "error" in body
        assert [c["name"] for c in body["candidates"]] == ["Bob and Nikesh"]


def test_wake_requires_instruction_and_owner(tmp_path, monkeypatch) -> None:
    settings, client = _client(tmp_path)
    _capture_wake(monkeypatch)
    with client:
        response = client.post(
            "/dashboard/api/conversations/wake",
            json={"target": BRAD_DM}, headers=_auth(settings))
        assert response.json()["error"] == "instruction required"

        # unseeded DB → no owner contact → fail closed
        response = client.post(
            "/dashboard/api/conversations/wake",
            json={"target": BRAD_DM, "instruction": "x"},
            headers=_auth(settings))
        assert "no default contact" in response.json()["error"]
