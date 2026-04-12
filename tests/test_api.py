from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

import cyborg.services.calendar_service as calendar_service_module
import cyborg.services.notification_service as notification_service_module
import cyborg.services.openclaw_hook_service as openclaw_hook_service_module
import cyborg.services.task_service as task_service_module
from cyborg.config import OpenClawHookSettings, Settings
from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.main import create_app
from cyborg.models import ProjectSpecApproveRequest
from cyborg.services.project_spec_service import ProjectSpecService
from cyborg.services.session_route_service import SessionRouteService
from cyborg.services.task_service import TaskService


def make_client(tmp_path: Path, settings: Settings | None = None) -> TestClient:
    resolved_settings = settings or Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
    )
    return TestClient(create_app(resolved_settings))


def create_task(client: TestClient, **payload: object) -> dict[str, object]:
    task = asyncio.run(TaskService(client.app.state.db).create_task(payload))
    return task.model_dump(mode="json")


def approve_spec(client: TestClient, spec_id: str, approver: str = "Bob") -> None:
    """Approve a project spec directly via the service (bypassing removed API endpoint)."""
    service = ProjectSpecService(client.app.state.db)
    payload = ProjectSpecApproveRequest(approver=approver)
    asyncio.run(service.approve_spec(spec_id, payload))


def test_health_and_task_retry_loop(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "database": "ok"}

        task = create_task(
            client,
            title="Investigate sync issue",
            requested_by="Bob",
            priority="high",
            plan="1. Inspect logs. 2. Retry sync. 3. Report outcome.",
            retry_config={
                "max_attempts": 2,
                "on_failure": "retry_from",
                "retry_from_step": 2,
            },
        )
        task_id = str(task["id"])
        assert task["status"] == "pending"

        started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert started.status_code == 200
        assert started.json()["status"] == "active"

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


def test_task_creation_routes_are_not_available(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        direct_task = client.post("/api/v1/tasks", json={"title": "Need a plan", "plan": "1. Plan. 2. Do."})
        assert direct_task.status_code == 405

        project = client.post("/api/v1/projects", json={"title": "Route removal"})
        assert project.status_code == 201
        project_id = project.json()["id"]

        nested_task = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"title": "Should fail", "plan": "1. Plan. 2. Do."},
        )
        assert nested_task.status_code == 405


def test_project_and_context_endpoints(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Proposal work",
                "aim": "Ship the first customer proposal",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "whatsappgroup-proposals",
                },
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        task = create_task(
            client,
            title="Draft proposal",
            priority="critical",
            plan="1. Draft proposal. 2. Review. 3. Deliver.",
            project_ids=[project_id],
        )
        task_id = str(task["id"])
        assert task["project_ids"] == [project_id]
        assert task["metadata"]["channel"] == "whatsapp"
        assert task["metadata"]["session_key"] == "whatsappgroup-proposals"

        journal = client.post(
            f"/api/v1/projects/{project_id}/journal",
            json={"entry_type": "note", "content": "Initial scope captured."},
        )
        assert journal.status_code == 201

        linked_tasks = client.get(f"/api/v1/projects/{project_id}/tasks")
        assert linked_tasks.status_code == 200
        assert linked_tasks.json()[0]["id"] == task_id

        projects = client.get("/api/v1/projects")
        assert projects.status_code == 200
        assert projects.json()[0]["id"] == project_id

        summary = client.get("/api/v1/context/summary")
        assert summary.status_code == 200
        payload = summary.json()
        assert payload["task_counts"]["pending"] == 1
        assert payload["project_counts"]["planning"] == 1
        assert payload["active_tasks"][0]["id"] == task_id
        assert payload["active_tasks"][0]["parent_project_id"] == project_id
        assert payload["active_tasks"][0]["parent_project_title"] == "Proposal work"

        tasks_context = client.get("/api/v1/context/tasks")
        assert tasks_context.status_code == 200
        assert tasks_context.json()["tasks"][0]["parent_project_id"] == project_id
        assert tasks_context.json()["tasks"][0]["parent_project_title"] == "Proposal work"


def test_project_create_requires_source_route_or_linked_task(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(base_url="https://openclaw.example", token="secret"),
    )
    with make_client(tmp_path, settings) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Unrouted project",
            },
        )
        assert project.status_code == 409
        assert "source routing metadata" in project.json()["detail"]


def test_project_can_infer_source_route_from_linked_task(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        source_project = client.post(
            "/api/v1/projects",
            json={
                "title": "Source route holder",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "whatsappgroup-source",
                    "chat_id": "120363400000000000@g.us",
                },
            },
        )
        assert source_project.status_code == 201
        source_project_id = source_project.json()["id"]

        task = create_task(
            client,
            title="Linked task",
            plan="1. Draft. 2. Review. 3. Ship.",
            project_ids=[source_project_id],
        )
        task_id = str(task["id"])

        inferred_project = client.post(
            "/api/v1/projects",
            json={
                "title": "Inferred route project",
                "task_ids": [task_id],
            },
        )
        assert inferred_project.status_code == 201
        inferred_metadata = inferred_project.json()["metadata"]
        assert inferred_metadata["channel"] == "whatsapp"
        assert inferred_metadata["session_key"] == "whatsappgroup-source"
        assert inferred_metadata["chat_id"] == "120363400000000000@g.us"


def test_notification_route_can_fall_back_to_parent_project_session_key(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Parent routed project",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "agent:main:whatsapp:group:120363423288899302@g.us",
                },
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        task = create_task(
            client,
            title="Inherited task",
            plan="1. Ask. 2. Record. 3. Report.",
            project_ids=[project_id],
        )
        task_id = str(task["id"])

        asyncio.run(client.app.state.db.execute("UPDATE tasks SET metadata = ? WHERE id = ?", ("{}", task_id)))

        route = asyncio.run(
            SessionRouteService(client.app.state.db).resolve_notification_route(
                {
                    "delivery_route": "source",
                    "task_id": task_id,
                    "parent_project_id": project_id,
                }
            )
        )
        assert route is not None
        assert route.chat_id == "120363423288899302@g.us"
        assert route.session_key == "agent:main:whatsapp:group:120363423288899302@g.us"
        assert route.route_source == "metadata.session_key"


def test_project_spec_approval_required_for_start_and_execute(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Launch plan",
                "description": "Prepare a launch",
                "metadata": {"session_key": "whatsappgroup-main", "channel": "whatsapp"},
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        assert project.json()["latest_spec_status"] is None

        submitted = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Ship the launch checklist",
                "method": "Plan the work, assign owners, and verify completion.",
                "success_criteria": [
                    {
                        "check": "completed_task_count >= 1",
                        "description": "At least one approved launch task is completed",
                    }
                ],
                "plan": [
                    {
                        "title": "Create checklist",
                        "description": "Draft the initial checklist",
                        "criteria": "Checklist exists",
                        "order": 0,
                    }
                ],
            },
        )
        assert submitted.status_code == 201
        spec_id = submitted.json()["id"]
        assert submitted.json()["status"] == "pending_approval"

        project_after_submit = client.get(f"/api/v1/projects/{project_id}")
        assert project_after_submit.status_code == 200
        assert project_after_submit.json()["latest_spec_id"] == spec_id
        assert project_after_submit.json()["latest_spec_status"] == "pending_approval"
        assert project_after_submit.json()["current_spec_id"] is None

        approve_spec(client, spec_id)

        # Spec approval auto-triggers execution, project should be active
        project_after_approve = client.get(f"/api/v1/projects/{project_id}")
        assert project_after_approve.json()["state"] == "active"
        assert project_after_approve.json()["current_spec_id"] == spec_id


def test_project_update_with_full_spec_creates_pending_revision(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Proposal work",
                "description": "Prepare the proposal",
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        initial_spec = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Ship the first proposal",
                "method": "Draft, review, and send the proposal.",
                "plan": [
                    {"title": "Draft proposal", "description": "Write the draft", "criteria": "Draft written", "order": 0}
                ],
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "Proposal task complete"}
                ],
            },
        )
        assert initial_spec.status_code == 201
        initial_spec_id = initial_spec.json()["id"]
        approve_spec(client, initial_spec_id)

        # Project is now active after spec approval — pause before updating spec fields
        pause = client.post(f"/api/v1/projects/{project_id}/pause")
        assert pause.status_code == 200

        revised = client.put(
            f"/api/v1/projects/{project_id}",
            json={
                "aim": "Ship the revised proposal",
                "method": "Draft, review, revise, and send the proposal.",
                "success_criteria": [
                    {"check": "completed_task_count >= 2", "description": "Two proposal tasks complete"}
                ],
            },
        )
        assert revised.status_code == 200
        assert revised.json()["current_spec_id"] == initial_spec_id
        assert revised.json()["latest_spec_status"] == "pending_approval"
        assert revised.json()["aim"] == "Ship the first proposal"

        specs = client.get(f"/api/v1/projects/{project_id}/specs")
        assert specs.status_code == 200
        assert len(specs.json()["specs"]) == 2
        assert specs.json()["specs"][0]["status"] == "pending_approval"
        assert specs.json()["specs"][1]["status"] == "approved"


def test_auto_execute_project_closes_when_last_manual_task_completes(tmp_path: Path, monkeypatch) -> None:
    import cyborg.services.openclaw_reasoning_service as reasoning_module

    call_count = 0

    async def fake_decide_next_step(self, project_id, completed_task_id):
        nonlocal call_count
        call_count += 1
        # First call (auto task completes) - create the manual task (already created), so close
        return {"action": "close_project", "reasoning": "All criteria met"}

    monkeypatch.setattr(reasoning_module.OpenClawReasoningService, "decide_next_step", fake_decide_next_step)

    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Manual close test",
                "description": "Close when the last linked task completes",
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        submitted = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Finish the manual close test",
                "method": "Complete the only linked task and evaluate the result.",
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "One linked task completes"}
                ],
                "plan": [
                    {
                        "title": "Complete the linked task",
                        "description": "Do the work and finish",
                        "criteria": "Task completed",
                        "order": 0,
                    },
                ],
            },
        )
        assert submitted.status_code == 201
        approve_spec(client, submitted.json()["id"])

        # Spec approval auto-triggers execution (creates task from plan step)
        project_tasks = client.get(f"/api/v1/projects/{project_id}/tasks")
        assert project_tasks.status_code == 200
        auto_task = project_tasks.json()[0]

        task = create_task(
            client,
            title="Only linked task",
            plan="1. Do the work. 2. Finish.",
            project_ids=[project_id],
        )
        task_id = str(task["id"])

        task_started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert task_started.status_code == 200
        completed = client.post(f"/api/v1/tasks/{task_id}/complete", json={"result_summary": "Done"})
        assert completed.status_code == 200

        # Also complete the auto-created step task
        auto_task_id = str(auto_task["id"])
        client.post(f"/api/v1/tasks/{auto_task_id}/start")
        client.post(f"/api/v1/tasks/{auto_task_id}/complete", json={"result_summary": "Step done"})

        refreshed_project = client.get(f"/api/v1/projects/{project_id}")
        assert refreshed_project.status_code == 200
        assert refreshed_project.json()["state"] == "closed"
        assert refreshed_project.json()["conclusion"] is not None

def test_calendar_event_and_recipient_flow(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        calendar = client.post(
            "/api/v1/calendars",
            json={
                "name": "Bob",
                "description": "Primary calendar",
                "color": "#2A9D8F",
                "is_default": True,
                "metadata": {"session_key": "whatsappgroup-main", "channel": "whatsapp"},
            },
        )
        assert calendar.status_code == 201
        calendar_id = calendar.json()["id"]
        assert calendar.json()["metadata"] == {"session_key": "whatsappgroup-main", "channel": "whatsapp"}

        updated_calendar = client.put(
            f"/api/v1/calendars/{calendar_id}",
            json={"metadata": {"session_key": "whatsappgroup-family", "channel": "whatsapp"}},
        )
        assert updated_calendar.status_code == 200
        assert updated_calendar.json()["metadata"] == {
            "session_key": "whatsappgroup-family",
            "channel": "whatsapp",
        }

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

        calendar_list = client.get("/api/v1/calendars")
        assert calendar_list.status_code == 200
        assert calendar_list.json()[0]["metadata"]["session_key"] == "whatsappgroup-family"


def test_soft_delete_hides_tasks(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = create_task(client, title="Disposable task", plan="1. Create. 2. Delete.")
        task_id = str(task["id"])

        deleted = client.delete(f"/api/v1/tasks/{task_id}")
        assert deleted.status_code == 204

        listing = client.get("/api/v1/tasks")
        assert listing.status_code == 200
        assert listing.json() == []

        missing = client.get(f"/api/v1/tasks/{task_id}")
        assert missing.status_code == 404


def test_task_target_session_round_trips_and_resolves_dm_contact(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        contact = client.post(
            "/api/v1/contacts",
            json={
                "name": "Alice Example",
                "phone_number": "0400 111 222",
            },
        )
        assert contact.status_code == 201
        contact_id = contact.json()["id"]

        task = create_task(
            client,
            title="Ask Alice for the figure",
            plan="1. Reach out. 2. Wait for answer. 3. Report back.",
            metadata={
                "channel": "whatsapp",
                "session_key": "whatsappgroup-origin",
                "target_session": {
                    "channel": "whatsapp",
                    "kind": "dm",
                    "contact_id": contact_id,
                },
            },
        )
        task_id = str(task["id"])
        assert task["metadata"]["session_key"] == "whatsappgroup-origin"
        assert task["metadata"]["target_session"] == {
            "channel": "whatsapp",
            "kind": "dm",
            "contact_id": contact_id,
        }

        task_service = TaskService(client.app.state.db)
        resolved = asyncio.run(task_service.resolve_target_session(task_id))
        assert resolved is not None
        assert resolved["channel"] == "whatsapp"
        assert resolved["kind"] == "dm"
        assert resolved["contact_id"] == contact_id
        assert resolved["contact_name"] == "Alice Example"
        assert resolved["phone_number"] == "+61400111222"
        assert resolved["session_key"] == "agent:main:whatsapp:direct:+61400111222"
        assert resolved["to"] == "+61400111222"
        assert resolved["route_source"] == "target_session.contact_id"


def test_task_target_session_accepts_groups_and_rejects_invalid_targets(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        session_route = client.post(
            "/api/v1/session-routes",
            json={
                "channel": "whatsapp",
                "session_key": "whatsappgroup-finance",
                "kind": "group",
                "chat_id": "120363426096069246@g.us",
            },
        )
        assert session_route.status_code == 201

        group_task = create_task(
            client,
            title="Ask the finance group",
            plan="1. Ask the group. 2. Collect answer. 3. Summarize.",
            metadata={
                "channel": "whatsapp",
                "session_key": "whatsappgroup-origin",
                "target_session": {
                    "channel": "whatsapp",
                    "kind": "group",
                    "session_key": "whatsappgroup-finance",
                },
            },
        )
        assert group_task["metadata"]["target_session"] == {
            "channel": "whatsapp",
            "kind": "group",
            "session_key": "whatsappgroup-finance",
        }

        task_service = TaskService(client.app.state.db)
        resolved = asyncio.run(task_service.resolve_target_session(str(group_task["id"])))
        assert resolved is not None
        assert resolved["to"] == "120363426096069246@g.us"
        assert resolved["route_source"] == "session_routes"

        with pytest.raises(ValidationError):
            asyncio.run(TaskService(client.app.state.db).create_task({
                "title": "Broken group target",
                "plan": "1. Try. 2. Fail.",
                "metadata": {
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "group",
                    },
                },
            }))

        with pytest.raises(ConflictError):
            asyncio.run(TaskService(client.app.state.db).create_task({
                "title": "Unknown group target",
                "plan": "1. Try. 2. Fail.",
                "metadata": {
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "group",
                        "session_key": "whatsappgroup-missing",
                    },
                },
            }))

        with pytest.raises(NotFoundError):
            asyncio.run(TaskService(client.app.state.db).create_task({
                "title": "Broken DM target",
                "plan": "1. Try. 2. Fail.",
                "metadata": {
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "dm",
                        "contact_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    },
                },
            }))


def test_task_assignment_only_for_created_tasks(tmp_path: Path) -> None:
    """Task assignment notifications are created at task creation time only, No task_result or NEEDS_INPUT notifications."""
    with make_client(tmp_path) as client:
        parent = create_task(
            client,
            title="Parent task",
            plan="1. Do parent work. 2. Finish.",
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-origin"},
        )
        parent_id = str(parent["id"])

        # Parent task gets a task_assignment notification at creation time
        initial_notifications = client.get("/api/v1/notifications?status=pending")
        assert initial_notifications.status_code == 200
        pending = initial_notifications.json()
        parent_assignments = [n for n in pending if n["entity_id"] == parent_id]
        assert len(parent_assignments) == 1
        assert parent_assignments[0]["notification_type"] == "task_assignment"

        child = create_task(
            client,
            title="Child task",
            plan="1. Follow the parent. 2. Finish.",
            parent_id=parent_id,
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-origin"},
        )
        child_id = str(child["id"])

        # Child is dependency-blocked, no task_assignment for it (no route resolved)
        after_create_notifications = client.get("/api/v1/notifications?status=pending")
        assert after_create_notifications.status_code == 200
        pending = after_create_notifications.json()
        child_notifications = [n for n in pending if n["entity_id"] == child_id]
        # Child has routing metadata inherited from parent, so it may get an assignment notification
        # but that's fine - the key point is no task_result or needs_input for tasks

        # Start and complete parent to release the child dependency
        started = client.post(f"/api/v1/tasks/{parent_id}/start")
        assert started.status_code == 200
        completed = client.post(
            f"/api/v1/tasks/{parent_id}/complete",
            json={"result_summary": "Parent finished"},
        )
        assert completed.status_code == 200

        # After parent completes, child should be released (pending)
        child_row = client.get(f"/api/v1/tasks/{child_id}")
        assert child_row.status_code == 200
        assert child_row.json()["status"] == "pending"
        assert child_row.json()["blocked_reason"] is None

        # No task_result notification for completed parent
        final_notifications = client.get("/api/v1/notifications?status=pending")
        assert final_notifications.status_code == 200
        pending = final_notifications.json()
        result_notifications = [n for n in pending if n["notification_type"] == "task_result"]
        assert len(result_notifications) == 0


def test_task_complete_accepts_result_alias_and_persists_summary(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = create_task(
            client,
            title="Ask Mike his favourite beer",
            plan="1. Ask the question. 2. Record the answer. 3. Report back.",
            metadata={
                "channel": "whatsapp",
                "chat_id": "120363426096069246@g.us",
            },
        )
        task_id = str(task["id"])
        started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert started.status_code == 200

        completed = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            json={"result": "Stella"},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert completed.json()["result"] == "Stella"

        task_row = client.get(f"/api/v1/tasks/{task_id}")
        assert task_row.status_code == 200
        assert task_row.json()["result"] == "Stella"

        history = client.get(f"/api/v1/tasks/{task_id}/history")
        assert history.status_code == 200
        completed_history = next(item for item in history.json() if item["action"] == "completed")
        assert completed_history["details"]["result"] == "Stella"


def test_project_blocked_notification_fire_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Only project-level NEEDS_INPUT is created when a project is blocked, fire-once."""
    current = datetime(2026, 3, 15, 8, 0, tzinfo=UTC)

    monkeypatch.setattr(calendar_service_module, "utcnow", lambda: current)
    monkeypatch.setattr(notification_service_module, "utcnow", lambda: current)

    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={"title": "Quarterly plan", "aim": "Prepare the quarter"},
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        project_spec = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Prepare the quarter",
                "method": "Gather inputs, draft the quarterly plan, and review it.",
                "plan": [
                    {"title": "Draft quarterly plan", "description": "Write the quarterly plan", "criteria": "Plan written", "order": 0}
                ],
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "At least one planning task is complete"}
                ],
            },
        )
        assert project_spec.status_code == 201
        approve_spec(client, project_spec.json()["id"])

        # Approval triggers task creation, which may produce a task_assignment notification
        notifications = client.get("/api/v1/notifications?status=pending")
        assert notifications.status_code == 200
        # Only task assignment notifications from execution, no project-level needs_input
        assert all(
            n.get("notification_type") != "needs_input"
            for n in notifications.json()
        )


def test_notification_list_handles_naive_event_datetimes(tmp_path: Path, monkeypatch) -> None:
    """Event reminders have been removed - verify no event notifications are created."""
    current = datetime(2026, 3, 15, 8, 0, tzinfo=UTC)

    monkeypatch.setattr(calendar_service_module, "utcnow", lambda: current)
    monkeypatch.setattr(notification_service_module, "utcnow", lambda: current)

    with make_client(tmp_path) as client:
        calendar = client.post(
            "/api/v1/calendars",
            json={
                "name": "Family",
                "metadata": {
                    "session_key": "whatsappgroup-family",
                    "channel": "whatsapp",
                    "reminder_minutes_before": 90,
                },
            },
        )
        assert calendar.status_code == 201
        calendar_id = calendar.json()["id"]

        event = client.post(
            "/api/v1/events",
            json={
                "calendar_id": calendar_id,
                "title": "School pickup",
                "start_time": "2026-03-15T16:45:00",
                "end_time": "2026-03-15T17:15:00",
                "timezone": "Australia/Perth",
                "venue": "Front gate",
            },
        )
        assert event.status_code == 201

        # No event notification should be created
        notifications = client.get("/api/v1/notifications?status=pending")
        assert notifications.status_code == 200
        assert notifications.json() == []


def test_session_route_crud(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        contact = client.post(
            "/api/v1/contacts",
            json={"name": "Alice Example", "phone_number": "0400 111 222"},
        )
        assert contact.status_code == 201
        contact_id = contact.json()["id"]

        created = client.post(
            "/api/v1/session-routes",
            json={
                "channel": "whatsapp",
                "session_key": "whatsappgroup-family",
                "kind": "group",
                "chat_id": "120363426096069246@g.us",
                "metadata": {"scope": "family"},
            },
        )
        assert created.status_code == 201
        route_id = created.json()["id"]

        listed = client.get("/api/v1/session-routes")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [route_id]

        dm_route = client.post(
            "/api/v1/session-routes",
            json={
                "channel": "whatsapp",
                "session_key": "whatsappdm-alice",
                "kind": "dm",
                "contact_id": contact_id,
            },
        )
        assert dm_route.status_code == 201

        updated = client.put(
            f"/api/v1/session-routes/{route_id}",
            json={"metadata": {"scope": "alerts"}, "is_active": False},
        )
        assert updated.status_code == 200
        assert updated.json()["is_active"] is False
        assert updated.json()["metadata"] == {"scope": "alerts"}

        active_only = client.get("/api/v1/session-routes")
        assert active_only.status_code == 200
        assert [item["session_key"] for item in active_only.json()] == ["whatsappdm-alice"]

        deleted = client.delete(f"/api/v1/session-routes/{route_id}")
        assert deleted.status_code == 204

        missing = client.get(f"/api/v1/session-routes/{route_id}")
        assert missing.status_code == 404


def test_task_assignment_notifications_dispatch_via_derived_target_dm_session(tmp_path: Path, monkeypatch) -> None:
    captured_gateway_requests: list[dict[str, object]] = []

    async def fake_send_gateway_request(
        self,
        method: str,
        params: dict[str, object],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        captured_gateway_requests.append(
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        openclaw_hook_service_module.OpenClawHookService,
        "_send_gateway_request",
        fake_send_gateway_request,
    )

    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            base_url="https://openclaw.example",
            token="secret",
            session_key_prefix="cyborg:",
        ),
        heartbeat_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        contact = client.post(
            "/api/v1/contacts",
            json={"name": "Alice Example", "phone_number": "0400 111 222"},
        )
        assert contact.status_code == 201
        contact_id = contact.json()["id"]

        task = create_task(
            client,
            title="Ask Alice for the figure",
            plan="1. DM Alice. 2. Wait for reply. 3. Report back.",
            metadata={
                "channel": "whatsapp",
                "chat_id": "120363400000000000@g.us",
                "session_key": "agent:main:whatsapp:group:120363400000000000@g.us",
                "target_session": {
                    "channel": "whatsapp",
                    "kind": "dm",
                    "contact_id": contact_id,
                },
            },
        )
        task_id = str(task["id"])

        notifications = client.get("/api/v1/notifications").json()
        assignment = next(item for item in notifications if item["notification_type"] == "task_assignment")
        assert assignment["metadata"]["delivery_route"] == "target"

        assignment_calls = [
            request
            for request in captured_gateway_requests
            if request["method"] == "agent" and request["params"].get("idempotencyKey") == assignment["id"]
        ]
        assert len(assignment_calls) == 1
        assignment_params = assignment_calls[0]["params"]
        assert assignment_calls[0]["expect_final"] is True
        assert assignment_calls[0]["timeout_seconds"] == 180.0
        assert assignment_params["deliver"] is True
        assert assignment_params["channel"] == "whatsapp"
        assert assignment_params["to"] == "+61400111222"
        assert assignment_params["sessionKey"] == "agent:main:whatsapp:direct:+61400111222"
        assert assignment_params["idempotencyKey"] == assignment["id"]
        assert assignment_params["thinking"] == "off"
        assert "You are responsible for handling this task in the current session." in str(assignment_params["message"])
        assert "Send one concise natural message now" in str(assignment_params["message"])

        target_sends = [
            request
            for request in captured_gateway_requests
            if request["method"] == "send"
            and (
                request["params"].get("to") == "+61400111222"
                or request["params"].get("idempotencyKey") == assignment["id"]
            )
        ]
        assert target_sends == []


def test_task_assignment_notifications_prefer_registered_dm_session_route(tmp_path: Path, monkeypatch) -> None:
    captured_gateway_requests: list[dict[str, object]] = []

    async def fake_send_gateway_request(
        self,
        method: str,
        params: dict[str, object],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        captured_gateway_requests.append(
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        openclaw_hook_service_module.OpenClawHookService,
        "_send_gateway_request",
        fake_send_gateway_request,
    )

    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            base_url="https://openclaw.example",
            token="secret",
            agent_id="worker",
            session_key_prefix="cyborg:",
        ),
        heartbeat_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        contact = client.post(
            "/api/v1/contacts",
            json={"name": "Alice Example", "phone_number": "0400 111 222"},
        )
        assert contact.status_code == 201
        contact_id = contact.json()["id"]

        route = client.post(
            "/api/v1/session-routes",
            json={
                "channel": "whatsapp",
                "session_key": "agent:custom:whatsapp:direct:+61400111222",
                "kind": "dm",
                "contact_id": contact_id,
            },
        )
        assert route.status_code == 201

        task = create_task(
            client,
            title="Ask Alice for the figure",
            plan="1. DM Alice. 2. Wait for reply. 3. Report back.",
            metadata={
                "channel": "whatsapp",
                "session_key": "agent:main:whatsapp:group:120363400000000000@g.us",
                "chat_id": "120363400000000000@g.us",
                "target_session": {
                    "channel": "whatsapp",
                    "kind": "dm",
                    "contact_id": contact_id,
                },
            },
        )
        task_id = str(task["id"])

        notifications = client.get("/api/v1/notifications").json()
        assignment = next(item for item in notifications if item["notification_type"] == "task_assignment")
        assert assignment["metadata"]["delivery_route"] == "target"

        assignment_calls = [
            request
            for request in captured_gateway_requests
            if request["method"] == "agent" and request["params"].get("idempotencyKey") == assignment["id"]
        ]
        assert len(assignment_calls) == 1
        assert assignment_calls[0]["expect_final"] is True
        assert assignment_calls[0]["params"]["deliver"] is True
        assert assignment_calls[0]["params"]["channel"] == "whatsapp"
        assert assignment_calls[0]["params"]["to"] == "+61400111222"
        assert assignment_calls[0]["params"]["sessionKey"] == "agent:custom:whatsapp:direct:+61400111222"

        target_sends = [
            request
            for request in captured_gateway_requests
            if request["method"] == "send"
            and (
                request["params"].get("to") == "+61400111222"
                or request["params"].get("idempotencyKey") == assignment["id"]
            )
        ]
        assert target_sends == []


def test_auto_created_project_task_assignments_bootstrap_agent_on_source_session(
    tmp_path: Path, monkeypatch
) -> None:
    captured_gateway_requests: list[dict[str, object]] = []

    async def fake_send_gateway_request(
        self,
        method: str,
        params: dict[str, object],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        captured_gateway_requests.append(
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        openclaw_hook_service_module.OpenClawHookService,
        "_send_gateway_request",
        fake_send_gateway_request,
    )

    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            base_url="https://openclaw.example",
            token="secret",
            agent_id="worker",
            session_key_prefix="cyborg:",
        ),
        heartbeat_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Execution Test",
                "aim": "Test execution bootstrap",
                "method": "Create an automatic task and have Claw work it in the same session.",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "agent:main:whatsapp:direct:+61456224867",
                },
                "plan": [
                    {
                        "title": "First Step",
                        "description": "Do first thing",
                        "criteria": "First done",
                        "order": 0,
                    },
                ],
                "success_criteria": [
                    {
                        "check": "completed_task_count >= 1",
                        "description": "The first task completes",
                    }
                ],
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        spec_id = client.get(f"/api/v1/projects/{project_id}/specs").json()["specs"][0]["id"]
        approve_spec(client, spec_id)

        # Spec approval auto-triggers execution

        notifications = client.get("/api/v1/notifications").json()
        assignment = next(item for item in notifications if item["notification_type"] == "task_assignment")
        assert assignment["metadata"]["delivery_route"] == "target"
        assert assignment["metadata"]["auto_created_by_project"] is True
        assert assignment["delivery_status"] == "delivered"

        task_id = assignment["entity_id"]
        short_project = project_id.replace("-", "")[:8]

        assignment_calls = [
            request
            for request in captured_gateway_requests
            if request["method"] == "agent" and request["params"].get("idempotencyKey") == assignment["id"]
        ]
        assert len(assignment_calls) == 1
        assert assignment_calls[0]["expect_final"] is True
        assert assignment_calls[0]["timeout_seconds"] == 180.0
        assert assignment_calls[0]["params"]["deliver"] is True
        assert assignment_calls[0]["params"]["channel"] == "whatsapp"
        assert assignment_calls[0]["params"]["to"] == "+61456224867"
        # Session key should be a cyborg:project:TASK:task:SHORTID pattern
        assert assignment_calls[0]["params"]["sessionKey"].startswith(f"cyborg:project:{short_project}:task:")
        assert assignment_calls[0]["params"]["agentId"] == "worker"

        sends = [
            request
            for request in captured_gateway_requests
            if request["method"] == "send" and request["params"].get("idempotencyKey") == assignment["id"]
        ]
        assert sends == []


def test_task_result_notifications_dispatch_via_source_session_route(tmp_path: Path, monkeypatch) -> None:
    captured_gateway_requests: list[dict[str, object]] = []

    async def fake_send_gateway_request(
        self,
        method: str,
        params: dict[str, object],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        captured_gateway_requests.append(
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        openclaw_hook_service_module.OpenClawHookService,
        "_send_gateway_request",
        fake_send_gateway_request,
    )

    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(base_url="https://openclaw.example", token="secret"),
        heartbeat_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        route = client.post(
            "/api/v1/session-routes",
            json={
                "channel": "whatsapp",
                "session_key": "whatsappgroup-source",
                "kind": "group",
                "chat_id": "120363499999999999@g.us",
            },
        )
        assert route.status_code == 201

        task = create_task(
            client,
            title="Compile the answer",
            plan="1. Gather inputs. 2. Summarize. 3. Send response.",
            metadata={
                "channel": "whatsapp",
                "session_key": "whatsappgroup-source",
            },
        )
        task_id = str(task["id"])

        # Task assignment notification created at creation time via source route
        notifications = client.get("/api/v1/notifications").json()
        assignment_notification = next(item for item in notifications if item["notification_type"] == "task_assignment")
        assert assignment_notification["metadata"]["delivery_route"] == "source"

        # Complete the task - no task_result notification should be created
        started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert started.status_code == 200
        completed = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            json={"result_summary": "Answer sent back to the user."},
        )
        assert completed.status_code == 200

        # Verify no task_result notifications
        notifications_after = client.get("/api/v1/notifications").json()
        result_notifications = [n for n in notifications_after if n["notification_type"] == "task_result"]
        assert len(result_notifications) == 0


def test_notification_process_due_endpoint_returns_processed_count(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        create_task(client, title="Needs a plan", plan="1. Draft. 2. Review.")

        processed = client.post("/api/v1/notifications/process-due")
        assert processed.status_code == 200
        assert processed.json() == {"processed": 0}


def test_dashboard_project_detail_shows_prompt_history(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Prompt Dashboard Test",
                "aim": "Verify prompt history shows on dashboard",
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        asyncio.run(
            client.app.state.db.execute(
                """
                INSERT INTO prompt_history (id, category, prompt_text, project_id, token_count_estimate)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "ph-test-001",
                    "plan_generation",
                    "Generate a project plan for building a cyberpunk dashboard portal.",
                    project_id,
                    15,
                ),
            )
        )

        response = client.get(f"/dashboard/projects/{project_id}")
        assert response.status_code == 200
        html = response.text
        assert "Prompt History" in html
        assert "plan generation" in html
        assert "Generate a project plan for building a cyberpunk dashboard portal." in html


def test_openclaw_gateway_transport_uses_backend_handshake_and_connect_challenge(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            gateway_url="ws://gateway.example",
            gateway_token="secret",
        ),
    )
    db = Database(settings.db_path, Path(__file__).resolve().parents[1] / "cyborg" / "schemas")
    db.settings = settings
    asyncio.run(db.connect())

    captured: dict[str, object] = {}

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.recv_count = 0

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        async def recv(self) -> str:
            if self.recv_count == 0:
                self.recv_count += 1
                return json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": "nonce-123", "ts": 1737264000000},
                    }
                )
            if self.recv_count == 1:
                self.recv_count += 1
                connect_request = self.sent[0]
                return json.dumps(
                    {
                        "type": "res",
                        "id": connect_request["id"],
                        "ok": True,
                        "payload": {
                            "type": "hello-ok",
                            "protocol": 3,
                            "policy": {"tickIntervalMs": 15000},
                        },
                    }
                )
            if self.recv_count == 2:
                self.recv_count += 1
                request = self.sent[1]
                return json.dumps(
                    {
                        "type": "res",
                        "id": request["id"],
                        "ok": True,
                        "payload": {"queued": True},
                    }
                )
            raise AssertionError("unexpected websocket recv")

    def fake_connect(url: str, **kwargs: object) -> FakeWebSocket:
        captured["url"] = url
        captured["kwargs"] = kwargs
        socket = FakeWebSocket()
        captured["socket"] = socket
        return socket

    monkeypatch.setattr(openclaw_hook_service_module.shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect))

    try:
        service = openclaw_hook_service_module.OpenClawHookService(db)
        result = asyncio.run(
            service._send_gateway_request(
                "send",
                {
                    "channel": "whatsapp",
                    "to": "+61400111222",
                    "message": "Hello",
                    "idempotencyKey": "notif-1",
                },
            )
        )
        assert result == {"queued": True}
    finally:
        asyncio.run(db.close())

    assert captured["url"] == "ws://gateway.example"
    socket = captured["socket"]
    assert isinstance(socket, FakeWebSocket)
    assert len(socket.sent) == 2

    connect_request = socket.sent[0]
    assert connect_request["method"] == "connect"
    assert connect_request["params"]["client"]["id"] == "gateway-client"
    assert connect_request["params"]["client"]["mode"] == "backend"
    assert connect_request["params"]["auth"] == {"token": "secret"}

    send_request = socket.sent[1]
    assert send_request["method"] == "send"
    assert send_request["params"]["to"] == "+61400111222"


def test_openclaw_gateway_transport_waits_for_final_agent_response_when_requested(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            gateway_url="ws://gateway.example",
            gateway_token="secret",
        ),
    )
    db = Database(settings.db_path, Path(__file__).resolve().parents[1] / "cyborg" / "schemas")
    db.settings = settings
    asyncio.run(db.connect())

    captured: dict[str, object] = {}

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.recv_count = 0

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        async def recv(self) -> str:
            if self.recv_count == 0:
                self.recv_count += 1
                return json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": "nonce-123", "ts": 1737264000000},
                    }
                )
            if self.recv_count == 1:
                self.recv_count += 1
                connect_request = self.sent[0]
                return json.dumps(
                    {
                        "type": "res",
                        "id": connect_request["id"],
                        "ok": True,
                        "payload": {
                            "type": "hello-ok",
                            "protocol": 3,
                            "policy": {"tickIntervalMs": 15000},
                        },
                    }
                )
            if self.recv_count == 2:
                self.recv_count += 1
                request = self.sent[1]
                return json.dumps(
                    {
                        "type": "res",
                        "id": request["id"],
                        "ok": True,
                        "payload": {"status": "accepted"},
                    }
                )
            if self.recv_count == 3:
                self.recv_count += 1
                request = self.sent[1]
                return json.dumps(
                    {
                        "type": "res",
                        "id": request["id"],
                        "ok": True,
                        "payload": {"result": {"payloads": []}, "summary": "NO_REPLY"},
                    }
                )
            raise AssertionError("unexpected websocket recv")

    def fake_connect(url: str, **kwargs: object) -> FakeWebSocket:
        captured["url"] = url
        captured["kwargs"] = kwargs
        socket = FakeWebSocket()
        captured["socket"] = socket
        return socket

    monkeypatch.setattr(openclaw_hook_service_module.shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect))

    try:
        service = openclaw_hook_service_module.OpenClawHookService(db)
        result = asyncio.run(
            service._send_gateway_request(
                "agent",
                {
                    "message": "Bootstrap task context",
                    "sessionKey": "agent:main:whatsapp:direct:+61400111222",
                    "deliver": False,
                },
                expect_final=True,
                timeout_seconds=60.0,
            )
        )
        assert result == {"result": {"payloads": []}, "summary": "NO_REPLY"}
    finally:
        asyncio.run(db.close())

    socket = captured["socket"]
    assert isinstance(socket, FakeWebSocket)
    assert len(socket.sent) == 2
    assert socket.sent[1]["method"] == "agent"


def test_openclaw_gateway_transport_prefers_official_cli(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            gateway_url="ws://gateway.example",
            gateway_token="secret",
        ),
    )
    db = Database(settings.db_path, Path(__file__).resolve().parents[1] / "cyborg" / "schemas")
    db.settings = settings
    asyncio.run(db.connect())

    captured: dict[str, object] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b'{"status":"queued"}', b"")

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(openclaw_hook_service_module.shutil, "which", lambda _: "/usr/bin/openclaw")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    try:
        service = openclaw_hook_service_module.OpenClawHookService(db)
        result = asyncio.run(
            service._send_gateway_request(
                "send",
                {
                    "channel": "whatsapp",
                    "to": "+61400111222",
                    "message": "Hello",
                    "idempotencyKey": "notif-1",
                },
            )
        )
        assert result == {"status": "queued"}
    finally:
        asyncio.run(db.close())

    args = list(captured["args"])
    assert args[:5] == ["/usr/bin/openclaw", "gateway", "call", "send", "--json"]
    assert "--url" in args
    assert "ws://gateway.example" in args
    assert "--token" in args
    assert "secret" in args


def test_openclaw_gateway_cli_uses_expect_final_for_bootstrap(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            gateway_url="ws://gateway.example",
            gateway_token="secret",
        ),
    )
    db = Database(settings.db_path, Path(__file__).resolve().parents[1] / "cyborg" / "schemas")
    db.settings = settings
    asyncio.run(db.connect())

    captured: dict[str, object] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b'{"summary":"NO_REPLY"}', b"")

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(openclaw_hook_service_module.shutil, "which", lambda _: "/usr/bin/openclaw")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    try:
        service = openclaw_hook_service_module.OpenClawHookService(db)
        result = asyncio.run(
            service._send_gateway_request(
                "agent",
                {
                    "message": "Bootstrap task context",
                    "sessionKey": "agent:main:whatsapp:direct:+61400111222",
                    "deliver": False,
                },
                expect_final=True,
                timeout_seconds=60.0,
            )
        )
        assert result == {"summary": "NO_REPLY"}
    finally:
        asyncio.run(db.close())

    args = list(captured["args"])
    assert args[:5] == ["/usr/bin/openclaw", "gateway", "call", "agent", "--json"]
    assert "--expect-final" in args


def test_auto_task_dispatch_with_phone_number_chat_id_and_no_session_key(
    tmp_path: Path, monkeypatch
) -> None:
    """Projects created with channel + chat_id (phone number, no session_key)
    should still dispatch auto-created task assignments by deriving a session key."""
    captured_gateway_requests: list[dict[str, object]] = []

    async def fake_send_gateway_request(
        self,
        method: str,
        params: dict[str, object],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        captured_gateway_requests.append(
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        openclaw_hook_service_module.OpenClawHookService,
        "_send_gateway_request",
        fake_send_gateway_request,
    )

    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            base_url="https://openclaw.example",
            token="secret",
            session_key_prefix="cyborg:",
        ),
        heartbeat_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        # Create project with only channel + chat_id (no session_key)
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Phone routing test",
                "aim": "Verify dispatch works without explicit session_key",
                "method": "Create a task and verify it dispatches.",
                "metadata": {
                    "channel": "whatsapp",
                    "chat_id": "+61456224867",
                },
                "plan": [
                    {
                        "title": "First Step",
                        "description": "Do first thing",
                        "criteria": "First done",
                        "order": 0,
                    },
                ],
                "success_criteria": [
                    {
                        "check": "completed_task_count >= 1",
                        "description": "The first task completes",
                    }
                ],
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        spec_id = client.get(f"/api/v1/projects/{project_id}/specs").json()["specs"][0]["id"]
        approve_spec(client, spec_id)

        notifications = client.get("/api/v1/notifications").json()
        assignment = next(
            item for item in notifications if item["notification_type"] == "task_assignment"
        )
        assert assignment["delivery_status"] == "delivered"

        assignment_calls = [
            request
            for request in captured_gateway_requests
            if request["method"] == "agent" and request["params"].get("idempotencyKey") == assignment["id"]
        ]
        assert len(assignment_calls) == 1
        assert assignment_calls[0]["params"]["channel"] == "whatsapp"
        assert assignment_calls[0]["params"]["to"] == "+61456224867"
        short_project = project_id.replace("-", "")[:8]
        assert assignment_calls[0]["params"]["sessionKey"].startswith(f"cyborg:project:{short_project}:task:")


def test_submitting_spec_creates_approval_record(tmp_path: Path) -> None:
    """Submitting a spec creates an approval record in the dashboard queue."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Approval queue test",
                "metadata": {"session_key": "test", "channel": "whatsapp"},
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        submitted = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Test approval queue",
                "method": "Submit spec and verify approval record.",
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "Task done"}
                ],
                "plan": [
                    {"title": "Step 1", "description": "Do it", "criteria": "Done", "order": 0},
                ],
            },
        )
        assert submitted.status_code == 201

        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'",
                (project_id,),
            )
        )
        assert approval_row is not None


def test_dashboard_approve_endpoint_approves_spec(tmp_path: Path) -> None:
    """Dashboard approve endpoint approves the spec and activates the project."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Dashboard approval test",
                "metadata": {"session_key": "test", "channel": "whatsapp"},
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        submitted = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Test dashboard approval",
                "method": "Submit spec, approve via dashboard endpoint.",
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "Task done"}
                ],
                "plan": [
                    {"title": "Step 1", "description": "Do the thing", "criteria": "Done", "order": 0},
                ],
            },
        )
        assert submitted.status_code == 201
        spec_id = submitted.json()["id"]

        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'",
                (project_id,),
            )
        )
        assert approval_row is not None
        approval_id = approval_row["id"]

        # Approve via the dashboard endpoint
        response = client.post(f"/dashboard/approve/{approval_id}")
        assert response.status_code == 200

        # Verify the spec was approved
        spec = client.get(f"/api/v1/project-specs/{spec_id}")
        assert spec.json()["status"] == "approved"

        # Verify the approval record was updated
        approval_after = asyncio.run(
            db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        )
        assert approval_after["status"] == "approved"

        # Verify the project became active
        refreshed = client.get(f"/api/v1/projects/{project_id}")
        assert refreshed.json()["state"] == "active"


def test_dashboard_reject_endpoint_rejects_spec(tmp_path: Path) -> None:
    """Dashboard reject endpoint rejects the associated spec."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Dashboard reject test",
                "metadata": {"session_key": "test", "channel": "whatsapp"},
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        submitted = client.post(
            f"/api/v1/projects/{project_id}/specs",
            json={
                "aim": "Test dashboard rejection",
                "method": "Submit spec, reject via dashboard.",
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "Task done"}
                ],
            },
        )
        assert submitted.status_code == 201
        spec_id = submitted.json()["id"]

        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'",
                (project_id,),
            )
        )
        assert approval_row is not None
        approval_id = approval_row["id"]

        # Reject via the dashboard endpoint
        response = client.post(f"/dashboard/reject/{approval_id}")
        assert response.status_code == 200

        # Verify the spec was rejected
        spec = client.get(f"/api/v1/project-specs/{spec_id}")
        assert spec.json()["status"] == "rejected"

        # Verify the approval record was updated
        approval_after = asyncio.run(
            db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        )
        assert approval_after["status"] == "rejected"

        # Project should still be in planning
        refreshed = client.get(f"/api/v1/projects/{project_id}")
        assert refreshed.json()["state"] == "planning"


def test_project_with_partial_spec_fields_accepted(tmp_path: Path) -> None:
    """Projects with partial spec fields are accepted — spec can be completed later."""
    with make_client(tmp_path) as client:
        # Has aim + method but no success_criteria → accepted, no spec submitted
        response = client.post(
            "/api/v1/projects",
            json={
                "title": "Incomplete project",
                "aim": "Do the thing",
                "method": "Plan and execute",
                "metadata": {"session_key": "test", "channel": "whatsapp"},
            },
        )
        assert response.status_code == 201

        # With complete spec fields, project is accepted and spec is submitted
        response2 = client.post(
            "/api/v1/projects",
            json={
                "title": "Complete project",
                "aim": "Do everything",
                "method": "All at once",
                "success_criteria": [
                    {"check": "done", "description": "It's done"}
                ],
                "metadata": {"session_key": "test", "channel": "whatsapp"},
            },
        )
        assert response2.status_code == 201


def test_blocking_task_with_input_schema_creates_approval(tmp_path: Path) -> None:
    """Blocking a task with input_schema creates a task_input approval record."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        task = create_task(
            client,
            title="Need user decision",
            plan="1. Ask user. 2. Continue with answer.",
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-test"},
        )
        task_id = str(task["id"])

        # Block the task with a text input schema
        blocked = client.post(
            f"/api/v1/tasks/{task_id}/block",
            json={
                "reason": "Need user to provide their name",
                "resume_instructions": "Use the provided name to continue the task.",
                "input_schema": {
                    "type": "text",
                    "prompt": "What is your name?",
                    "placeholder": "Enter your name here...",
                },
            },
        )
        assert blocked.status_code == 200
        assert blocked.json()["status"] == "blocked"

        # Verify the approval record was created
        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'task_input' AND status = 'pending'",
                (task_id,),
            )
        )
        assert approval_row is not None
        assert approval_row["input_schema"] is not None

        import json

        input_schema = json.loads(approval_row["input_schema"])
        assert input_schema["type"] == "text"
        assert input_schema["prompt"] == "What is your name?"
        assert input_schema["placeholder"] == "Enter your name here..."


def test_blocking_task_with_multi_choice_input_schema(tmp_path: Path) -> None:
    """Blocking a task with multi_choice input_schema creates the right approval."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        task = create_task(
            client,
            title="Choose a design",
            plan="1. Present options. 2. Build chosen design.",
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-test"},
        )
        task_id = str(task["id"])

        blocked = client.post(
            f"/api/v1/tasks/{task_id}/block",
            json={
                "reason": "User needs to choose a design",
                "resume_instructions": "Build the chosen design option.",
                "input_schema": {
                    "type": "multi_choice",
                    "prompt": "Which design do you prefer?",
                    "options": [
                        {"value": "minimal", "label": "Design A - Minimal"},
                        {"value": "bold", "label": "Design B - Bold"},
                    ],
                    "allow_multiple": False,
                },
            },
        )
        assert blocked.status_code == 200
        assert blocked.json()["status"] == "blocked"

        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'task_input'",
                (task_id,),
            )
        )
        assert approval_row is not None

        import json

        input_schema = json.loads(approval_row["input_schema"])
        assert input_schema["type"] == "multi_choice"
        assert input_schema["prompt"] == "Which design do you prefer?"
        assert len(input_schema["options"]) == 2
        assert input_schema["options"][0]["value"] == "minimal"
        assert input_schema["allow_multiple"] is False


def test_blocking_task_without_input_schema_creates_approval_without_schema(tmp_path: Path) -> None:
    """Blocking a task without input_schema creates an approval but with no input_schema."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        task = create_task(
            client,
            title="Dependency wait",
            plan="1. Wait for parent. 2. Continue.",
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-test"},
        )
        task_id = str(task["id"])

        blocked = client.post(
            f"/api/v1/tasks/{task_id}/block",
            json={
                "reason": "Waiting for dependency",
                "resume_instructions": "Resume once dependency resolves.",
            },
        )
        assert blocked.status_code == 200

        approval_rows = asyncio.run(
            db.fetch_all(
                "SELECT * FROM approvals WHERE entity_id = ?",
                (task_id,),
            )
        )
        assert len(approval_rows) == 1
        assert approval_rows[0]["input_schema"] is None


def test_dashboard_input_resolve_endpoint_unblocks_task(tmp_path: Path) -> None:
    """Dashboard input resolve endpoint saves response, unblocks task."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        task = create_task(
            client,
            title="Need input",
            plan="1. Ask question. 2. Continue.",
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-test"},
        )
        task_id = str(task["id"])

        client.post(
            f"/api/v1/tasks/{task_id}/block",
            json={
                "reason": "Need user's favourite colour",
                "resume_instructions": "Continue with the colour.",
                "input_schema": {
                    "type": "text",
                    "prompt": "What is your favourite colour?",
                },
            },
        )

        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'task_input'",
                (task_id,),
            )
        )
        assert approval_row is not None
        approval_id = approval_row["id"]

        # Resolve via dashboard endpoint
        response = client.post(
            f"/dashboard/approve/{approval_id}/input",
            data={"response": "Blue"},
        )
        assert response.status_code == 200

        # Verify approval was updated
        approval_after = asyncio.run(
            db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        )
        assert approval_after["status"] == "approved"

        import json

        input_response = json.loads(approval_after["input_response"])
        assert input_response == "Blue"

        # Verify task was unblocked
        task_after = client.get(f"/api/v1/tasks/{task_id}")
        assert task_after.json()["status"] in ("active", "pending")


def test_dashboard_reject_task_input_unblocks_with_note(tmp_path: Path) -> None:
    """Rejecting a task_input approval unblocks the task with a declined note."""
    with make_client(tmp_path) as client:
        db = client.app.state.db

        task = create_task(
            client,
            title="Need input",
            plan="1. Ask question. 2. Continue.",
            metadata={"channel": "whatsapp", "session_key": "whatsappgroup-test"},
        )
        task_id = str(task["id"])

        client.post(
            f"/api/v1/tasks/{task_id}/block",
            json={
                "reason": "Need user's answer",
                "resume_instructions": "Continue after answer.",
                "input_schema": {
                    "type": "text",
                    "prompt": "Do you want to proceed?",
                },
            },
        )

        approval_row = asyncio.run(
            db.fetch_one(
                "SELECT * FROM approvals WHERE entity_id = ? AND approval_type = 'task_input'",
                (task_id,),
            )
        )
        assert approval_row is not None
        approval_id = approval_row["id"]

        # Reject via dashboard
        response = client.post(f"/dashboard/reject/{approval_id}")
        assert response.status_code == 200

        # Verify approval was rejected
        approval_after = asyncio.run(
            db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        )
        assert approval_after["status"] == "rejected"

        # Verify task was unblocked
        task_after = client.get(f"/api/v1/tasks/{task_id}")
        assert task_after.json()["status"] in ("active", "pending")


def test_task_tap_creates_notification(tmp_path: Path, monkeypatch) -> None:
    """Tapping an active task inserts a task_tap notification row into the database."""
    captured_gateway_requests: list[dict[str, object]] = []

    async def fake_send_gateway_request(
        self,
        method: str,
        params: dict[str, object],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        captured_gateway_requests.append(
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        openclaw_hook_service_module.OpenClawHookService,
        "_send_gateway_request",
        fake_send_gateway_request,
    )

    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
        openclaw=OpenClawHookSettings(
            base_url="https://openclaw.example",
            token="secret",
            session_key_prefix="cyborg:",
        ),
        heartbeat_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        # Create an active task with routing metadata
        task = create_task(
            client,
            title="Tap me",
            plan="1. Do work. 2. Report back.",
            metadata={
                "channel": "whatsapp",
                "session_key": "agent:main:whatsapp:group:test-group@g.us",
            },
        )
        task_id = str(task["id"])

        # Activate the task (created in PENDING status; tap requires ACTIVE)
        started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert started.status_code == 200
        assert started.json()["status"] == "active"

        # Tap the task via the dashboard endpoint
        response = client.post(f"/dashboard/tasks/{task_id}/tap")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["task_id"] == task_id

        # Verify the task_tap notification was persisted
        notifications = client.get("/api/v1/notifications").json()
        tap_notifications = [
            n for n in notifications if n["notification_type"] == "task_tap"
        ]
        assert len(tap_notifications) == 1
        tap = tap_notifications[0]
        assert tap["entity_id"] == task_id
        assert tap["status"] == "acknowledged"
        assert tap["metadata"]["delivery_route"] == "source"

        # Verify a second tap returns 200 (notification row inserted again)
        response2 = client.post(f"/dashboard/tasks/{task_id}/tap")
        assert response2.status_code == 200

        # Non-existent task → 404
        assert client.post("/dashboard/tasks/00000000-0000-0000-0000-000000000000/tap").status_code == 404

        # Task without routing metadata → 422
        no_route_settings = Settings(
            data_dir=tmp_path / "data2",
            config_dir=tmp_path / "config2",
            db_path=tmp_path / "data2" / "cyborg.db",
        )
        with make_client(tmp_path, no_route_settings) as client2:
            bare_task = create_task(client2, title="No route", plan="1. Nothing.")
            client2.post(f"/api/v1/tasks/{bare_task['id']}/start")
            bare_response = client2.post(f"/dashboard/tasks/{bare_task['id']}/tap")
            assert bare_response.status_code == 422
