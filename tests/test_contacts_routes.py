"""Contacts API route order (2026-10-14): GET/DELETE /default were
registered after /{contact_id}, so 'default' was parsed as a UUID and the
endpoints answered 422 — `bob contact get-default` never worked."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app


def test_default_contact_routes_are_reachable(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db",
    )
    with TestClient(create_app(settings)) as client:
        headers = {"X-Dashboard-Secret": settings.resolved_api_secret}
        response = client.get("/api/v1/contacts/default", headers=headers)
        assert response.status_code == 404
        assert response.json()["detail"] == "No default contact configured"

        created = client.post("/api/v1/contacts", headers=headers,
                              json={"name": "Mike", "phone_number": "+61400000000"})
        assert created.status_code == 201
        contact_id = created.json()["id"]
        assert client.put(f"/api/v1/contacts/{contact_id}/set-default",
                          headers=headers).status_code == 200

        response = client.get("/api/v1/contacts/default", headers=headers)
        assert response.status_code == 200
        assert response.json()["id"] == contact_id

        assert client.delete("/api/v1/contacts/default",
                             headers=headers).status_code == 204
