"""/voice/realtime test mode is operator-only (2026-10-12).

/voice rides the public funnel. Test mode starts an arbitrary prompt on our
OpenAI Realtime key with the realtime tool set (contact lookup included), so
a start without the API token must be refused before any bridge is built.
The authorized probe omits ``instructions`` so the handler stops at the
next check instead of dialling OpenAI."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from server.config import OpenAISettings, Settings
from server.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db",
        openai=OpenAISettings(api_key="sk-test-not-real"),
    )


def _first_reply(client: TestClient, frame: dict, **kw) -> dict:
    with client.websocket_connect("/voice/realtime", **kw) as ws:
        ws.send_text(json.dumps(frame))
        return json.loads(ws.receive_text())


def test_test_mode_refused_without_token(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        reply = _first_reply(client, {"type": "start", "instructions": "say hi"})
        assert reply["type"] == "error" and "operator token" in reply["message"]


def test_test_mode_accepts_frame_secret_or_query_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    token = settings.resolved_api_secret
    with TestClient(create_app(settings)) as client:
        # Past the gate: the handler's next complaint is the missing prompt.
        reply = _first_reply(client, {"type": "start", "secret": token})
        assert "instructions" in reply["message"], reply
        with client.websocket_connect(f"/voice/realtime?secret={token}") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            assert "instructions" in json.loads(ws.receive_text())["message"]


def test_wrong_secret_refused(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        reply = _first_reply(client, {"type": "start", "secret": "nope",
                                      "instructions": "x"})
        assert "operator token" in reply["message"]


def test_session_mode_unaffected_by_the_gate(tmp_path: Path) -> None:
    """Persona/session links keep their own token auth — an unknown session
    id is rejected as invalid, not as 'needs operator token'."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        reply = _first_reply(client, {"type": "start", "session_id": "bogus"})
        assert "invalid" in reply["message"] and "operator" not in reply["message"]
