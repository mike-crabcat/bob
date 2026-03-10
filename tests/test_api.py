from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cyborg.config import Settings
from cyborg.main import create_app


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
    )
    return TestClient(create_app(settings))


def test_health_and_task_retry_loop(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "database": "ok"}

        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Investigate sync issue",
                "requested_by": "Bob",
                "priority": "high",
                "retry_config": {
                    "max_attempts": 2,
                    "on_failure": "retry_from",
                    "retry_from_step": 2,
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        step1 = client.post(
            f"/api/v1/tasks/{task_id}/steps",
            json={"step_number": 1, "description": "Inspect logs", "status": "completed"},
        )
        step2 = client.post(
            f"/api/v1/tasks/{task_id}/steps",
            json={"step_number": 2, "description": "Retry sync", "status": "active"},
        )
        step3 = client.post(
            f"/api/v1/tasks/{task_id}/steps",
            json={"step_number": 3, "description": "Report outcome", "status": "pending"},
        )
        assert step1.status_code == 201
        assert step2.status_code == 201
        assert step3.status_code == 201

        failed = client.post(
            f"/api/v1/tasks/{task_id}/fail",
            json={"details": {"reason": "network timeout"}, "result": "timeout"},
        )
        assert failed.status_code == 200
        failed_body = failed.json()
        assert failed_body["status"] == "active"
        assert failed_body["retry_config"]["current_attempt"] == 1

        steps = client.get(f"/api/v1/tasks/{task_id}/steps")
        assert steps.status_code == 200
        steps_body = steps.json()
        assert steps_body[0]["status"] == "completed"
        assert steps_body[1]["status"] == "active"
        assert steps_body[2]["status"] == "pending"

        history = client.get(f"/api/v1/tasks/{task_id}/history")
        actions = [item["action"] for item in history.json()]
        assert "failed" in actions
        assert "retry_from_step" in actions


def test_project_and_context_endpoints(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = client.post("/api/v1/tasks", json={"title": "Draft proposal", "priority": "critical"})
        assert task.status_code == 201
        task_id = task.json()["id"]

        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Proposal work",
                "aim": "Ship the first customer proposal",
                "task_ids": [task_id],
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        journal = client.post(
            f"/api/v1/projects/{project_id}/journal",
            json={"entry_type": "note", "content": "Initial scope captured."},
        )
        assert journal.status_code == 201

        linked_tasks = client.get(f"/api/v1/projects/{project_id}/tasks")
        assert linked_tasks.status_code == 200
        assert linked_tasks.json()[0]["id"] == task_id

        summary = client.get("/api/v1/context/summary")
        assert summary.status_code == 200
        payload = summary.json()
        assert payload["task_counts"]["pending"] == 1
        assert payload["project_counts"]["planning"] == 1
        assert payload["active_tasks"][0]["id"] == task_id


def test_calendar_event_and_recipient_flow(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        calendar = client.post(
            "/api/v1/calendars",
            json={"name": "Bob", "description": "Primary calendar", "color": "#2A9D8F", "is_default": True},
        )
        assert calendar.status_code == 201
        calendar_id = calendar.json()["id"]

        start = datetime.now(UTC) + timedelta(days=1)
        end = start + timedelta(hours=1)
        event = client.post(
            "/api/v1/events",
            json={
                "calendar_id": calendar_id,
                "title": "Planning session",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "timezone": "UTC",
            },
        )
        assert event.status_code == 201
        event_id = event.json()["id"]

        confirmed = client.post(f"/api/v1/events/{event_id}/confirm")
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"

        recipient = client.post(
            f"/api/v1/events/{event_id}/recipients",
            json={
                "recipient_type": "email",
                "recipient_address": "bob@example.com",
                "name": "Bob",
            },
        )
        assert recipient.status_code == 201
        recipient_id = recipient.json()["id"]

        updated = client.put(
            f"/api/v1/events/{event_id}/recipients/{recipient_id}",
            json={"status": "confirmed"},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "confirmed"

        context_calendar = client.get("/api/v1/context/calendar")
        assert context_calendar.status_code == 200
        assert context_calendar.json()["events"][0]["id"] == event_id


def test_soft_delete_hides_tasks(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = client.post("/api/v1/tasks", json={"title": "Disposable task"})
        task_id = task.json()["id"]

        deleted = client.delete(f"/api/v1/tasks/{task_id}")
        assert deleted.status_code == 204

        listing = client.get("/api/v1/tasks")
        assert listing.status_code == 200
        assert listing.json() == []

        missing = client.get(f"/api/v1/tasks/{task_id}")
        assert missing.status_code == 404
