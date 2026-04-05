"""Tests for project execution service."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cyborg.config import Settings
from cyborg.main import create_app
from cyborg.services.task_service import TaskService

PROJECT_ROUTE_METADATA = {
    "channel": "whatsapp",
    "session_key": "agent:test:whatsapp:group:120363400000000000@g.us",
}


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
    )
    return TestClient(create_app(settings))


def approve_latest_project_spec(client: TestClient, project_id: str, approver: str = "Bob") -> dict:
    specs = client.get(f"/api/v1/projects/{project_id}/specs")
    assert specs.status_code == 200
    spec_id = specs.json()["specs"][0]["id"]
    approved = client.post(f"/api/v1/project-specs/{spec_id}/approve", json={"approver": approver})
    assert approved.status_code == 200
    return approved.json()


def create_task(client: TestClient, **payload: object) -> dict[str, object]:
    task = asyncio.run(TaskService(client.app.state.db).create_task(payload))
    return task.model_dump(mode="json")


class TestProjectExecution:
    """Tests for self-executing project functionality."""

    def test_create_project_with_plan_and_criteria(self, tmp_path: Path) -> None:
        """Test creating a project with plan steps and success criteria."""
        with make_client(tmp_path) as client:
            response = client.post(
                "/api/v1/projects",
                json={
                    "title": "Test Execution Project",
                    "aim": "Build a test API",
                    "method": "Define the implementation approach and then execute each step.",
                    "description": "Testing auto-execution",
                    "auto_execute": True,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Setup",
                            "description": "Create project structure",
                            "criteria": "Structure created",
                            "order": 0,
                        },
                        {
                            "title": "Implement",
                            "description": "Build the API",
                            "criteria": "API working",
                            "order": 1,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "completed_task_count >= 2",
                            "description": "All steps completed",
                        },
                    ],
                },
            )
            assert response.status_code == 201
            data = response.json()
            assert data["title"] == "Test Execution Project"
            assert data["auto_execute"] is True
            assert len(data["plan"]) == 2
            assert len(data["success_criteria"]) == 1
            assert data["state"] == "planning"
            assert data["current_spec_id"] is None
            assert data["latest_spec_status"] == "pending_approval"

    def test_start_project_execution_creates_first_task(self, tmp_path: Path) -> None:
        """Test starting project execution creates the first task."""
        with make_client(tmp_path) as client:
            # Create project
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Execution Test",
                    "aim": "Test execution",
                    "method": "Define the first task and execute it.",
                    "auto_execute": True,
                    "metadata": {
                        "channel": "whatsapp",
                        "session_key": "whatsappgroup-execution",
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
            ).json()
            project_id = project["id"]
            approve_latest_project_spec(client, project_id)

            # Spec approval auto-triggers execution
            project_after = client.get(f"/api/v1/projects/{project_id}").json()
            assert project_after["state"] == "active"

            # Verify task was created
            tasks = client.get(f"/api/v1/projects/{project_id}/tasks").json()
            assert len(tasks) == 1
            assert tasks[0]["title"] == "Step 1: First Step"
            assert tasks[0]["status"] == "active"
            assert tasks[0]["started_at"] is not None
            assert "Success criteria:" in tasks[0]["plan"]
            assert tasks[0]["metadata"]["project_step_index"] == 0
            assert tasks[0]["metadata"]["channel"] == "whatsapp"
            assert tasks[0]["metadata"]["session_key"] == "whatsappgroup-execution"

    def test_task_completion_triggers_next_task(self, tmp_path: Path) -> None:
        """Test completing a task creates the next task."""
        with make_client(tmp_path) as client:
            # Create and start project
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Multi-step Test",
                    "aim": "Test multi-step",
                    "method": "Work through the plan one step at a time.",
                    "auto_execute": True,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Step 1",
                            "description": "Do step 1",
                            "criteria": "Step 1 done",
                            "order": 0,
                        },
                        {
                            "title": "Step 2",
                            "description": "Do step 2",
                            "criteria": "Step 2 done",
                            "order": 1,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "completed_task_count >= 2",
                            "description": "Both tasks complete",
                        }
                    ],
                },
            ).json()
            project_id = project["id"]
            approve_latest_project_spec(client, project_id)

            # Spec approval auto-triggers execution

            # Get first task
            tasks = client.get(f"/api/v1/projects/{project_id}/tasks").json()
            first_task_id = tasks[0]["id"]

            # Complete first task (already auto-started)
            client.post(
                f"/api/v1/tasks/{first_task_id}/complete",
                json={"result_summary": "Step 1 completed"},
            )

            # Verify second task was created
            tasks = client.get(f"/api/v1/projects/{project_id}/tasks").json()
            assert len(tasks) == 2
            step2 = [t for t in tasks if "Step 2" in t["title"]][0]
            assert step2["status"] == "active"
            assert step2["started_at"] is not None
            assert step2["metadata"]["project_step_index"] == 1

    def test_all_tasks_complete_auto_closes_project(self, tmp_path: Path) -> None:
        """Test completing all tasks auto-closes the project."""
        with make_client(tmp_path) as client:
            # Create and start project
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Auto-close Test",
                    "aim": "Test auto-close",
                    "method": "Complete the planned task and evaluate the result.",
                    "auto_execute": True,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Only Step",
                            "description": "Do the only step",
                            "criteria": "Done",
                            "order": 0,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "completed_task_count >= 1",
                            "description": "One step completed",
                        },
                    ],
                },
            ).json()
            project_id = project["id"]
            approve_latest_project_spec(client, project_id)

            # Spec approval auto-triggers execution

            # Get and complete the only task
            tasks = client.get(f"/api/v1/projects/{project_id}/tasks").json()
            task_id = tasks[0]["id"]

            # Complete task (already auto-started)
            client.post(
                f"/api/v1/tasks/{task_id}/complete",
                json={"result_summary": "Completed"},
            )

            # Verify project is closed
            project = client.get(f"/api/v1/projects/{project_id}").json()
            assert project["state"] == "closed"
            assert project["conclusion"] is not None
            assert "Accomplishments" in project["conclusion"]
            assert "Success Criteria Met" in project["conclusion"]

            notifications = client.get("/api/v1/notifications")
            assert notifications.status_code == 200
            project_result = next(
                item
                for item in notifications.json()
                if item["entity_type"] == "project" and item["notification_type"] == "project_result"
            )
            assert project_result["entity_id"] == project_id
            assert project_result["metadata"]["project_id"] == project_id
            assert project_result["metadata"]["delivery_route"] == "source"
            assert project_result["title"] == "Project completed: Auto-close Test"
            assert "Accomplishments" in project_result["message"]

    def test_unmet_success_criteria_generate_single_follow_up_task(self, tmp_path: Path) -> None:
        """Test that unmet criteria create one follow-up task after the last open task completes."""
        with make_client(tmp_path) as client:
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Autonomy Gap Test",
                    "aim": "Reach two completed tasks",
                    "method": "Finish the planned step, then generate exactly one follow-up task if more work is needed.",
                    "auto_execute": True,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Initial step",
                            "description": "Do the first thing",
                            "criteria": "Initial work done",
                            "order": 0,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "completed_task_count >= 2",
                            "description": "Two tasks have been completed",
                        },
                    ],
                },
            )
            assert project.status_code == 201
            project_id = project.json()["id"]
            approve_latest_project_spec(client, project_id)

            # Spec approval auto-triggers execution

            first_task = client.get(f"/api/v1/projects/{project_id}/tasks").json()[0]
            first_task_id = first_task["id"]
            completed = client.post(
                f"/api/v1/tasks/{first_task_id}/complete",
                json={"result_summary": "Initial step finished"},
            )
            assert completed.status_code == 200

            project_tasks = client.get(f"/api/v1/projects/{project_id}/tasks")
            assert project_tasks.status_code == 200
            tasks = project_tasks.json()
            assert len(tasks) == 2

            follow_up_tasks = [
                task for task in tasks if task["metadata"].get("autonomy_reason") == "unmet_success_criteria"
            ]
            assert len(follow_up_tasks) == 1
            follow_up = follow_up_tasks[0]
            assert follow_up["status"] == "active"
            assert follow_up["started_at"] is not None
            assert follow_up["project_ids"] == [project_id]
            assert follow_up["metadata"]["auto_created_by_project"] is True
            assert follow_up["metadata"]["autonomy_cycle_completed_task_count"] == 1
            assert follow_up["metadata"]["autonomy_criterion_check"] == "completed_task_count >= 2"
            assert follow_up["metadata"]["autonomy_criterion_description"] == "Two tasks have been completed"
            assert "Success criterion check: completed_task_count >= 2" in follow_up["plan"]

            project_row = client.get(f"/api/v1/projects/{project_id}")
            assert project_row.status_code == 200
            assert project_row.json()["state"] == "active"

            notifications = client.get("/api/v1/notifications")
            assert notifications.status_code == 200
            pending_notifications = notifications.json()
            assert any(
                item["entity_id"] == follow_up["id"] and item["notification_type"] == "task_assignment"
                for item in pending_notifications
            )
            assert not any(
                item["entity_id"] == follow_up["id"] and item["notification_type"] == "needs_input"
                for item in pending_notifications
            )

    def test_follow_up_task_completion_closes_project_when_criteria_are_met(self, tmp_path: Path) -> None:
        """Test that a generated follow-up task can satisfy the project and trigger closure."""
        with make_client(tmp_path) as client:
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Autonomy Close Test",
                    "aim": "Reach two completed tasks",
                    "method": "Complete the planned step, then the generated follow-up.",
                    "auto_execute": True,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Initial step",
                            "description": "Do the first thing",
                            "criteria": "Initial work done",
                            "order": 0,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "completed_task_count >= 2",
                            "description": "Two tasks have been completed",
                        },
                    ],
                },
            )
            assert project.status_code == 201
            project_id = project.json()["id"]
            approve_latest_project_spec(client, project_id)

            # Spec approval auto-triggers execution

            first_task = client.get(f"/api/v1/projects/{project_id}/tasks").json()[0]
            first_task_id = first_task["id"]
            assert client.post(
                f"/api/v1/tasks/{first_task_id}/complete",
                json={"result_summary": "Initial step finished"},
            ).status_code == 200

            tasks_after_first_completion = client.get(f"/api/v1/projects/{project_id}/tasks")
            assert tasks_after_first_completion.status_code == 200
            follow_up = next(
                task
                for task in tasks_after_first_completion.json()
                if task["metadata"].get("autonomy_reason") == "unmet_success_criteria"
            )

            assert client.post(
                f"/api/v1/tasks/{follow_up['id']}/complete",
                json={"result_summary": "Follow-up completed"},
            ).status_code == 200

            closed_project = client.get(f"/api/v1/projects/{project_id}")
            assert closed_project.status_code == 200
            closed = closed_project.json()
            assert closed["state"] == "closed"
            assert closed["conclusion"] is not None
            assert "Success Criteria Met" in closed["conclusion"]

            notifications = client.get("/api/v1/notifications")
            assert notifications.status_code == 200
            project_results = [
                item
                for item in notifications.json()
                if item["entity_type"] == "project" and item["notification_type"] == "project_result"
            ]
            assert len(project_results) == 1
            assert project_results[0]["entity_id"] == project_id
            assert project_results[0]["metadata"]["project_state"] == "closed"

    def test_non_auto_execute_project_no_progression(self, tmp_path: Path) -> None:
        """Test that non-auto-execute projects don't auto-progress."""
        with make_client(tmp_path) as client:
            # Create project without auto_execute
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Manual Project",
                    "aim": "Manual work",
                    "auto_execute": False,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Step 1",
                            "description": "Do step 1",
                            "criteria": "Done",
                            "order": 0,
                        },
                    ],
                },
            ).json()
            project_id = project["id"]

            # Create a manual task
            task = create_task(
                client,
                title="Manual Task",
                description="A manual task",
                plan="1. Do the manual task. 2. Report the result.",
                priority="medium",
                project_ids=[project_id],
            )
            task_id = str(task["id"])

            # Complete the task
            client.post(f"/api/v1/tasks/{task_id}/start")
            client.post(f"/api/v1/tasks/{task_id}/complete")

            # Verify no new task was auto-created
            tasks = client.get(f"/api/v1/projects/{project_id}/tasks").json()
            assert len(tasks) == 1

    def test_project_update_plan_and_criteria(self, tmp_path: Path) -> None:
        """Test updating project plan and success criteria."""
        with make_client(tmp_path) as client:
            # Create project
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Update Test",
                    "aim": "Test updates",
                    "metadata": PROJECT_ROUTE_METADATA,
                },
            ).json()
            project_id = project["id"]

            # Update with plan and criteria
            response = client.put(
                f"/api/v1/projects/{project_id}",
                json={
                    "plan": [
                        {
                            "title": "New Step",
                            "description": "A new step",
                            "criteria": "Done",
                            "order": 0,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "task_count > 0",
                            "description": "Has tasks",
                        },
                    ],
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["plan"]) == 1
            assert len(data["success_criteria"]) == 1

    def test_evaluate_endpoint(self, tmp_path: Path) -> None:
        """Test the evaluate endpoint for manual completion check on non-auto-execute projects."""
        with make_client(tmp_path) as client:
            # Create project with auto_execute=False so the autonomy checkpoint
            # won't auto-close it — the evaluate endpoint must be called manually.
            project = client.post(
                "/api/v1/projects",
                json={
                    "title": "Evaluate Test",
                    "aim": "Test evaluate",
                    "method": "Create and complete a manual project task, then evaluate the project.",
                    "auto_execute": False,
                    "metadata": PROJECT_ROUTE_METADATA,
                    "plan": [
                        {
                            "title": "Manual step",
                            "description": "Do the manual task",
                            "criteria": "Done",
                            "order": 0,
                        },
                    ],
                    "success_criteria": [
                        {
                            "check": "completed_task_count >= 1",
                            "description": "One done",
                        },
                    ],
                },
            ).json()
            project_id = project["id"]

            # Spec approval triggers execution even for non-auto projects
            # (start_project_execution creates the first task regardless)
            approve_latest_project_spec(client, project_id)

            # Get the auto-created step task from execution
            project_tasks = client.get(f"/api/v1/projects/{project_id}/tasks").json()
            auto_task_id = project_tasks[0]["id"]

            # Create and complete a task manually
            task = create_task(
                client,
                title="Manual Task",
                description="A task",
                plan="1. Do the task. 2. Evaluate the result.",
                priority="medium",
                project_ids=[project_id],
            )
            task_id = str(task["id"])

            # Evaluate before completion - should not close (criteria not met)
            result = client.post(f"/api/v1/projects/{project_id}/evaluate")
            assert result.json() is None

            # Complete both tasks
            for tid in [task_id, auto_task_id]:
                client.post(f"/api/v1/tasks/{tid}/start")
                client.post(f"/api/v1/tasks/{tid}/complete", json={"result_summary": "Done"})

            # Evaluate after completion - should close (criteria met)
            result = client.post(f"/api/v1/projects/{project_id}/evaluate")
            assert result.json() is not None
            assert result.json()["state"] == "closed"
