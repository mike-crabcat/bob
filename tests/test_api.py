from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient

import cyborg.services.calendar_service as calendar_service_module
import cyborg.services.notification_service as notification_service_module
import cyborg.services.openclaw_hook_service as openclaw_hook_service_module
import cyborg.services.task_service as task_service_module
from cyborg.config import OpenClawHookSettings, Settings
from cyborg.database import Database
from cyborg.main import create_app
from cyborg.services.task_service import TaskService


def make_client(tmp_path: Path, settings: Settings | None = None) -> TestClient:
    resolved_settings = settings or Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
    )
    return TestClient(create_app(resolved_settings))


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
                "plan": "1. Inspect logs. 2. Retry sync. 3. Report outcome.",
                "retry_config": {
                    "max_attempts": 2,
                    "on_failure": "retry_from",
                    "retry_from_step": 2,
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]
        assert task.json()["status"] == "planning"

        pre_plan_start = client.post(f"/api/v1/tasks/{task_id}/start")
        assert pre_plan_start.status_code == 409

        plan = client.get(f"/api/v1/tasks/{task_id}/plans")
        assert plan.status_code == 200
        plan_id = plan.json()["plans"][0]["id"]

        approved = client.post(
            f"/api/v1/plans/{plan_id}/approve",
            json={"approver": "Bob"},
        )
        assert approved.status_code == 200

        pending_task = client.get(f"/api/v1/tasks/{task_id}")
        assert pending_task.status_code == 200
        assert pending_task.json()["status"] == "pending"

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


def test_task_planning_gate_requires_plan_approval(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = client.post("/api/v1/tasks", json={"title": "Need a plan", "plan": "1. Plan the work. 2. Do the work."})
        assert task.status_code == 201
        task_id = task.json()["id"]
        assert task.json()["status"] == "planning"

        missing_plan = client.post("/api/v1/tasks", json={"title": "Missing plan"})
        assert missing_plan.status_code == 422

        update_pending = client.put(f"/api/v1/tasks/{task_id}", json={"status": "pending"})
        assert update_pending.status_code == 409

        plans = client.get(f"/api/v1/tasks/{task_id}/plans")
        assert plans.status_code == 200
        plan_id = plans.json()["plans"][0]["id"]

        start_without_approval = client.post(f"/api/v1/tasks/{task_id}/start")
        assert start_without_approval.status_code == 409

        approved = client.post(
            f"/api/v1/plans/{plan_id}/approve",
            json={"approver": "Bob"},
        )
        assert approved.status_code == 200

        task_after_approval = client.get(f"/api/v1/tasks/{task_id}")
        assert task_after_approval.status_code == 200
        assert task_after_approval.json()["status"] == "pending"

        started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert started.status_code == 200
        assert started.json()["status"] == "active"


def test_project_and_context_endpoints(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Proposal work",
                "aim": "Ship the first customer proposal",
            },
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        task = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={
                "title": "Draft proposal",
                "priority": "critical",
                "plan": "1. Draft proposal. 2. Review. 3. Deliver.",
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]
        assert task.json()["project_ids"] == [project_id]

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
        assert payload["task_counts"]["planning"] == 1
        assert payload["project_counts"]["planning"] == 1
        assert payload["active_tasks"][0]["id"] == task_id
        assert payload["active_tasks"][0]["parent_project_id"] == project_id
        assert payload["active_tasks"][0]["parent_project_title"] == "Proposal work"

        tasks_context = client.get("/api/v1/context/tasks")
        assert tasks_context.status_code == 200
        assert tasks_context.json()["tasks"][0]["parent_project_id"] == project_id
        assert tasks_context.json()["tasks"][0]["parent_project_title"] == "Proposal work"


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

        start_without_spec = client.post(f"/api/v1/projects/{project_id}/start")
        assert start_without_spec.status_code == 409

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

        execute_without_approval = client.post(f"/api/v1/projects/{project_id}/execute")
        assert execute_without_approval.status_code == 409

        project_after_submit = client.get(f"/api/v1/projects/{project_id}")
        assert project_after_submit.status_code == 200
        assert project_after_submit.json()["latest_spec_id"] == spec_id
        assert project_after_submit.json()["latest_spec_status"] == "pending_approval"
        assert project_after_submit.json()["current_spec_id"] is None

        approved = client.post(f"/api/v1/project-specs/{spec_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["is_current"] is True

        started = client.post(f"/api/v1/projects/{project_id}/start")
        assert started.status_code == 200
        assert started.json()["state"] == "active"
        assert started.json()["current_spec_id"] == spec_id


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
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "Proposal task complete"}
                ],
            },
        )
        assert initial_spec.status_code == 201
        initial_spec_id = initial_spec.json()["id"]
        approved = client.post(f"/api/v1/project-specs/{initial_spec_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200

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


def test_auto_execute_project_closes_when_last_manual_task_completes(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "title": "Manual close test",
                "description": "Close when the last linked task completes",
                "auto_execute": True,
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
            },
        )
        assert submitted.status_code == 201
        approved = client.post(
            f"/api/v1/project-specs/{submitted.json()['id']}/approve",
            json={"approver": "Bob"},
        )
        assert approved.status_code == 200

        started_project = client.post(f"/api/v1/projects/{project_id}/start")
        assert started_project.status_code == 200

        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Only linked task",
                "plan": "1. Do the work. 2. Finish.",
                "project_ids": [project_id],
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        plan_id = client.get(f"/api/v1/tasks/{task_id}/plans").json()["plans"][0]["id"]
        task_approved = client.post(f"/api/v1/plans/{plan_id}/approve", json={"approver": "Bob"})
        assert task_approved.status_code == 200
        task_started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert task_started.status_code == 200
        completed = client.post(f"/api/v1/tasks/{task_id}/complete", json={"result_summary": "Done"})
        assert completed.status_code == 200

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
        task = client.post("/api/v1/tasks", json={"title": "Disposable task", "plan": "1. Create. 2. Delete."})
        task_id = task.json()["id"]

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

        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Ask Alice for the figure",
                "plan": "1. Reach out. 2. Wait for answer. 3. Report back.",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "whatsappgroup-origin",
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "dm",
                        "contact_id": contact_id,
                    },
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]
        assert task.json()["metadata"]["session_key"] == "whatsappgroup-origin"
        assert task.json()["metadata"]["target_session"] == {
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

        group_task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Ask the finance group",
                "plan": "1. Ask the group. 2. Collect answer. 3. Summarize.",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "whatsappgroup-origin",
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "group",
                        "session_key": "whatsappgroup-finance",
                    },
                },
            },
        )
        assert group_task.status_code == 201
        assert group_task.json()["metadata"]["target_session"] == {
            "channel": "whatsapp",
            "kind": "group",
            "session_key": "whatsappgroup-finance",
        }

        task_service = TaskService(client.app.state.db)
        resolved = asyncio.run(task_service.resolve_target_session(group_task.json()["id"]))
        assert resolved is not None
        assert resolved["to"] == "120363426096069246@g.us"
        assert resolved["route_source"] == "session_routes"

        missing_group_route = client.post(
            "/api/v1/tasks",
            json={
                "title": "Broken group target",
                "plan": "1. Try. 2. Fail.",
                "metadata": {
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "group",
                    },
                },
            },
        )
        assert missing_group_route.status_code == 422

        unresolved_group_route = client.post(
            "/api/v1/tasks",
            json={
                "title": "Unknown group target",
                "plan": "1. Try. 2. Fail.",
                "metadata": {
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "group",
                        "session_key": "whatsappgroup-missing",
                    },
                },
            },
        )
        assert unresolved_group_route.status_code == 409

        missing_contact = client.post(
            "/api/v1/tasks",
            json={
                "title": "Broken DM target",
                "plan": "1. Try. 2. Fail.",
                "metadata": {
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "dm",
                        "contact_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    },
                },
            },
        )
        assert missing_contact.status_code == 404


def test_task_notifications_skip_dependency_blocked_tasks(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        parent = client.post(
            "/api/v1/tasks",
            json={
                "title": "Parent task",
                "plan": "1. Do parent work. 2. Finish.",
                "metadata": {"channel": "whatsapp", "session_key": "whatsappgroup-origin"},
            },
        )
        assert parent.status_code == 201
        parent_id = parent.json()["id"]

        child = client.post(
            "/api/v1/tasks",
            json={
                "title": "Child task",
                "plan": "1. Follow the parent. 2. Finish.",
                "parent_id": parent_id,
                "metadata": {"channel": "whatsapp", "session_key": "whatsappgroup-origin"},
            },
        )
        assert child.status_code == 201
        child_id = child.json()["id"]

        initial_notifications = client.get("/api/v1/notifications")
        assert initial_notifications.status_code == 200
        assert [item["entity_id"] for item in initial_notifications.json()] == [parent_id]

        parent_plan_id = client.get(f"/api/v1/tasks/{parent_id}/plans").json()["plans"][0]["id"]
        child_plan_id = client.get(f"/api/v1/tasks/{child_id}/plans").json()["plans"][0]["id"]
        approved_child = client.post(f"/api/v1/plans/{child_plan_id}/approve", json={"approver": "Bob"})
        assert approved_child.status_code == 200
        child_after_approval = client.get(f"/api/v1/tasks/{child_id}")
        assert child_after_approval.status_code == 200
        assert child_after_approval.json()["status"] == "blocked"

        approved = client.post(f"/api/v1/plans/{parent_plan_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200
        started = client.post(f"/api/v1/tasks/{parent_id}/start")
        assert started.status_code == 200
        completed = client.post(
            f"/api/v1/tasks/{parent_id}/complete",
            json={"result_summary": "Parent finished"},
        )
        assert completed.status_code == 200

        follow_up_notifications = client.get("/api/v1/notifications")
        assert follow_up_notifications.status_code == 200
        pending = follow_up_notifications.json()
        assert {(item["entity_id"], item["notification_type"]) for item in pending} == {
            (child_id, "task_assignment"),
            (parent_id, "task_result"),
        }

        child_row = client.get(f"/api/v1/tasks/{child_id}")
        assert child_row.status_code == 200
        assert child_row.json()["status"] == "pending"
        assert child_row.json()["notification_count"] == 1
        assert child_row.json()["blocked_reason"] is None

        resolved = client.get("/api/v1/notifications")
        assert resolved.status_code == 200
        assert {(item["entity_id"], item["notification_type"]) for item in resolved.json()} == {
            (child_id, "task_assignment"),
            (parent_id, "task_result"),
        }


def test_task_notifications_repeat_daily_then_stop(tmp_path: Path, monkeypatch) -> None:
    current = datetime(2026, 3, 15, 8, 0, tzinfo=UTC)

    monkeypatch.setattr(task_service_module, "utcnow", lambda: current)
    monkeypatch.setattr(notification_service_module, "utcnow", lambda: current)

    with make_client(tmp_path) as client:
        task = client.post(
            "/api/v1/tasks",
            json={"title": "Review numbers", "plan": "1. Prepare. 2. Review. 3. Approve."},
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        expected_sequences = [1, 2, 3, 4]
        for expected_sequence in expected_sequences:
            notifications = client.get("/api/v1/notifications")
            assert notifications.status_code == 200
            pending = notifications.json()
            assert len(pending) == 1
            assert pending[0]["entity_id"] == task_id
            assert pending[0]["sequence_number"] == expected_sequence

            acknowledged = client.post(
                f"/api/v1/notifications/{pending[0]['id']}/acknowledge",
                json={"acknowledged_by": "client"},
            )
            assert acknowledged.status_code == 200

            current = current + timedelta(days=1, minutes=1)

        final_pending = client.get("/api/v1/notifications")
        assert final_pending.status_code == 200
        assert final_pending.json() == []


def test_task_complete_accepts_result_alias_and_persists_summary(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Ask Mike his favourite beer",
                "plan": "1. Ask the question. 2. Record the answer. 3. Report back.",
                "metadata": {
                    "channel": "whatsapp",
                    "chat_id": "120363426096069246@g.us",
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        plan_id = client.get(f"/api/v1/tasks/{task_id}/plans").json()["plans"][0]["id"]
        approved = client.post(f"/api/v1/plans/{plan_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200
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

        notifications = client.get("/api/v1/notifications")
        assert notifications.status_code == 200
        result_notification = next(
            item for item in notifications.json() if item["entity_id"] == task_id and item["notification_type"] == "task_result"
        )
        assert result_notification["message"] == "Stella"

def test_project_and_event_notifications_are_persisted_until_acknowledged(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
                "success_criteria": [
                    {"check": "completed_task_count >= 1", "description": "At least one planning task is complete"}
                ],
            },
        )
        assert project_spec.status_code == 201
        approved_spec = client.post(
            f"/api/v1/project-specs/{project_spec.json()['id']}/approve",
            json={"approver": "Bob"},
        )
        assert approved_spec.status_code == 200

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
                "start_time": (current + timedelta(minutes=45)).isoformat(),
                "end_time": (current + timedelta(minutes=75)).isoformat(),
                "timezone": "UTC",
                "venue": "Front gate",
            },
        )
        assert event.status_code == 201

        notifications = client.get("/api/v1/notifications")
        assert notifications.status_code == 200
        pending = notifications.json()
        assert {(item["entity_type"], item["notification_type"]) for item in pending} == {
            ("event", "event_reminder"),
        }

        event_notification = next(item for item in pending if item["entity_type"] == "event")
        assert event_notification["metadata"]["session_key"] == "whatsappgroup-family"
        assert event_notification["metadata"]["channel"] == "whatsapp"

        started = client.post(f"/api/v1/projects/{project_id}/start")
        assert started.status_code == 200

        after_project_start = client.get("/api/v1/notifications")
        assert after_project_start.status_code == 200
        pending_after_start = after_project_start.json()
        assert len(pending_after_start) == 1
        assert pending_after_start[0]["entity_type"] == "event"

        acknowledged = client.post(
            f"/api/v1/notifications/{event_notification['id']}/acknowledge",
            json={"acknowledged_by": "client"},
        )
        assert acknowledged.status_code == 200

        after_ack = client.get("/api/v1/notifications")
        assert after_ack.status_code == 200
        assert after_ack.json() == []


def test_notification_list_handles_naive_event_datetimes(tmp_path: Path, monkeypatch) -> None:
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

        first_notifications = client.get("/api/v1/notifications")
        assert first_notifications.status_code == 200
        first_pending = first_notifications.json()
        assert len(first_pending) == 1
        assert first_pending[0]["entity_type"] == "event"
        assert first_pending[0]["metadata"]["session_key"] == "whatsappgroup-family"

        second_notifications = client.get("/api/v1/notifications")
        assert second_notifications.status_code == 200
        second_pending = second_notifications.json()
        assert len(second_pending) == 1
        assert second_pending[0]["id"] == first_pending[0]["id"]


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
        notification_dispatch_interval_seconds=0,
    )

    with make_client(tmp_path, settings) as client:
        contact = client.post(
            "/api/v1/contacts",
            json={"name": "Alice Example", "phone_number": "0400 111 222"},
        )
        assert contact.status_code == 201
        contact_id = contact.json()["id"]

        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Ask Alice for the figure",
                "plan": "1. DM Alice. 2. Wait for reply. 3. Report back.",
                "metadata": {
                    "channel": "whatsapp",
                    "chat_id": "120363400000000000@g.us",
                    "session_key": "agent:main:whatsapp:group:120363400000000000@g.us",
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "dm",
                        "contact_id": contact_id,
                    },
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        plan_id = client.get(f"/api/v1/tasks/{task_id}/plans").json()["plans"][0]["id"]
        approved = client.post(f"/api/v1/plans/{plan_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200

        notifications = client.get("/api/v1/notifications").json()
        assignment = next(item for item in notifications if item["notification_type"] == "task_assignment")
        assert assignment["metadata"]["delivery_route"] == "target"

        assignment_calls = [request for request in captured_gateway_requests if request["method"] == "agent"]
        assert len(assignment_calls) == 1
        assignment_params = assignment_calls[0]["params"]
        assert assignment_calls[0]["expect_final"] is True
        assert assignment_calls[0]["timeout_seconds"] == 60.0
        assert assignment_params["deliver"] is True
        assert assignment_params["channel"] == "whatsapp"
        assert assignment_params["to"] == "+61400111222"
        assert assignment_params["sessionKey"] == "agent:main:whatsapp:direct:+61400111222"
        assert assignment_params["idempotencyKey"] == assignment["id"]
        assert assignment_params["thinking"] == "minimal"
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
        notification_dispatch_interval_seconds=0,
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

        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Ask Alice for the figure",
                "plan": "1. DM Alice. 2. Wait for reply. 3. Report back.",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "agent:main:whatsapp:group:120363400000000000@g.us",
                    "chat_id": "120363400000000000@g.us",
                    "target_session": {
                        "channel": "whatsapp",
                        "kind": "dm",
                        "contact_id": contact_id,
                    },
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        plan_id = client.get(f"/api/v1/tasks/{task_id}/plans").json()["plans"][0]["id"]
        approved = client.post(f"/api/v1/plans/{plan_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200

        notifications = client.get("/api/v1/notifications").json()
        assignment = next(item for item in notifications if item["notification_type"] == "task_assignment")
        assert assignment["delivery_status"] == "delivered"

        assignment_calls = [request for request in captured_gateway_requests if request["method"] == "agent"]
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
        notification_dispatch_interval_seconds=0,
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

        task = client.post(
            "/api/v1/tasks",
            json={
                "title": "Compile the answer",
                "plan": "1. Gather inputs. 2. Summarize. 3. Send response.",
                "metadata": {
                    "channel": "whatsapp",
                    "session_key": "whatsappgroup-source",
                },
            },
        )
        assert task.status_code == 201
        task_id = task.json()["id"]

        plan_id = client.get(f"/api/v1/tasks/{task_id}/plans").json()["plans"][0]["id"]
        approved = client.post(f"/api/v1/plans/{plan_id}/approve", json={"approver": "Bob"})
        assert approved.status_code == 200
        started = client.post(f"/api/v1/tasks/{task_id}/start")
        assert started.status_code == 200
        completed = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            json={"result_summary": "Answer sent back to the user."},
        )
        assert completed.status_code == 200

        notifications = client.get("/api/v1/notifications").json()
        result_notification = next(item for item in notifications if item["notification_type"] == "task_result")
        assert result_notification["metadata"]["delivery_route"] == "source"

        source_sends = [
            request
            for request in captured_gateway_requests
            if request["method"] == "send" and request["params"].get("to") == "120363499999999999@g.us"
        ]
        assert source_sends
        assert source_sends[0]["params"]["sessionKey"] == "whatsappgroup-source"
        assert any("Task completed: Compile the answer" in str(request["params"]["message"]) for request in source_sends)
        assert all(request["method"] != "agent" for request in captured_gateway_requests)


def test_notification_process_due_endpoint_returns_processed_count(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task = client.post(
            "/api/v1/tasks",
            json={"title": "Needs a plan", "plan": "1. Draft. 2. Review."},
        )
        assert task.status_code == 201

        processed = client.post("/api/v1/notifications/process-due")
        assert processed.status_code == 200
        assert processed.json() == {"processed": 0}


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
