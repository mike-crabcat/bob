"""API token gate tests — regression for the 2026-08-22 incident where the
agent's bash tool POSTed to /phone/call anonymously and placed a real call."""

from __future__ import annotations

import stat
from pathlib import Path

from fastapi.testclient import TestClient

from bob_server.config import PhoneSettings, Settings
from bob_server.main import create_app


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        phone=PhoneSettings(enabled=True),
        **overrides,
    )


def test_post_denied_without_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        # Empty 'to' means the handler would return a 200 error dict with zero
        # Twilio side effects if reached — so anything other than 401 is a leak.
        response = client.post("/phone/call", json={})
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized: valid API token required"}


def test_post_allowed_with_token_via_each_transport(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    token = settings.resolved_api_secret
    with TestClient(create_app(settings)) as client:
        # Header transport
        response = client.post(
            "/phone/call", json={}, headers={"X-Dashboard-Secret": token}
        )
        assert response.status_code == 200
        assert response.json() == {"error": "Missing 'to' phone number"}

        # Bearer transport
        response = client.post(
            "/phone/call", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200

        # Cookie transport (what the dashboard SPA relies on)
        response = client.post(
            "/phone/call", json={}, cookies={"bob_dashboard_secret": token}
        )
        assert response.status_code == 200

        # Legacy query-param transport (SPA's ?secret=)
        response = client.post(f"/phone/call?secret={token}", json={})
        assert response.status_code == 200

        # Wrong token is rejected on every transport
        response = client.post(
            "/phone/call", json={}, headers={"X-Dashboard-Secret": "not-the-token"}
        )
        assert response.status_code == 401


def test_twilio_callbacks_exempt(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        # Twilio posts form data; unauthenticated must reach the handler.
        response = client.post("/phone/twiml", data={})
        assert response.status_code != 401
        response = client.post("/phone/status", data={})
        assert response.status_code != 401


def test_voice_log_exempt(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.post("/voice/log", json={"level": "info", "message": "x"})
        assert response.status_code != 401


def test_safe_methods_unaffected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


def test_kill_switch_disables_gate_and_secret_generation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_auth_disabled=True)
    with TestClient(create_app(settings)) as client:
        response = client.post("/phone/call", json={})
        assert response.status_code == 200
        assert response.json() == {"error": "Missing 'to' phone number"}
    assert not settings.api_auth_enabled
    assert not (tmp_path / "data" / "api_secret").exists()


def test_generated_secret_persisted_with_mode_0600_and_stable(tmp_path: Path) -> None:
    first = make_settings(tmp_path)
    secret = first.resolved_api_secret
    assert secret

    path = tmp_path / "data" / "api_secret"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # A fresh Settings over the same data_dir resolves the same secret.
    second = make_settings(tmp_path)
    assert second.resolved_api_secret == secret


def test_explicit_dashboard_secret_wins_and_writes_no_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, dashboard_secret="explicit-secret")
    assert settings.resolved_api_secret == "explicit-secret"
    assert not (tmp_path / "data" / "api_secret").exists()

    with TestClient(create_app(settings)) as client:
        assert client.post("/phone/call", json={}).status_code == 401
        response = client.post(
            "/phone/call", json={}, headers={"X-Dashboard-Secret": "explicit-secret"}
        )
        assert response.status_code == 200


def test_dashboard_auth_endpoint_sets_cookie_for_valid_token(tmp_path: Path) -> None:
    """The one-URL phone flow: GET /dashboard/api/auth?secret=<token> must set
    the bob_dashboard_secret cookie the SPA reads, and reject wrong tokens."""
    settings = make_settings(tmp_path)
    token = settings.resolved_api_secret
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/dashboard/api/auth?secret={token}")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        set_cookie = response.headers["set-cookie"]
        assert "bob_dashboard_secret=" in set_cookie
        assert "httponly" not in set_cookie.lower()  # SPA reads document.cookie

        # The freshly set cookie authenticates a gated POST (TestClient jar).
        assert client.post("/phone/call", json={}).status_code == 200

        # Wrong token: 401, no cookie (fresh client — no valid cookie jar).
        with TestClient(create_app(settings)) as bare:
            bad = bare.get("/dashboard/api/auth?secret=not-the-token")
            assert bad.status_code == 401
            assert "set-cookie" not in bad.headers


def test_dashboard_auth_endpoint_when_gate_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_auth_disabled=True)
    with TestClient(create_app(settings)) as client:
        response = client.get("/dashboard/api/auth")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "set-cookie" not in response.headers


def test_dashboard_auth_accepts_raw_pasted_plus_in_query(tmp_path: Path) -> None:
    """Phone flow: a raw-pasted (unencoded) URL carries a base64 token's '+'
    literally; query parsing decodes it to a space. The endpoint must accept
    the space-restored form too."""
    settings = make_settings(tmp_path, dashboard_secret="abc+def/ghi=")
    with TestClient(create_app(settings)) as client:
        # httpx encodes params properly; simulate a raw paste by sending the
        # decoded-what-the-server-would-see form directly.
        assert client.get("/dashboard/api/auth", params={"secret": "abc def/ghi="}).status_code == 200
        client.cookies.clear()
        assert client.get("/dashboard/api/auth", params={"secret": "abc+def/ghi="}).status_code == 200
        client.cookies.clear()
        assert client.get("/dashboard/api/auth", params={"secret": "abcXdef/ghi="}).status_code == 401
