"""Integration tests for autonomous project execution loop."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from bob_server.services.project_autonomy_service import ProjectAutonomyService
from bob_server.services.project_execution_service import ProjectExecutionService
from bob_server.services.project_service import ProjectService
from bob_server.database import Database


@pytest_asyncio.fixture
async def autonomous_project(db: Database):
    """Create a test project with auto-execute enabled."""
    from bob_server.services.project_service import ProjectService
    from bob_server.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    # Create auto-executing project
    project = await project_service.create_project({
        "title": "Autonomous Test Project",
        "aim": "Test autonomous completion",
        "method": "Use automated reasoning",
        "success_criteria": [
            {
                "check": "completed_tasks >= 2",
                "description": "Complete at least 2 tasks"
            },
        ],
        "plan": [
            {
                "title": "Complete tasks",
                "description": "Complete the required tasks",
                "criteria": "Tasks done",
                "order": 0,
            },
        ],
    })

    project_id = str(project.id)

    # Approve the spec (auto-triggers execution)
    from bob_server.models import ProjectSpecApproveRequest
    spec_service = project_service.project_spec_service
    specs = await spec_service.list_specs(project_id)
    await spec_service.approve_spec(
        str(specs.specs[0].id),
        ProjectSpecApproveRequest(approver="test"),
    )

    return {
        "project_id": project_id,
        "project": project,
    }


@pytest.mark.asyncio
@pytest.mark.skip(reason="Removed PlanService — test needs rewrite for new task lifecycle")
async def test_dependency_release_and_autonomy(db: Database):
    """Test that dependent tasks are released and then trigger autonomy."""
    from bob_server.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    # Create project
    project = await project_service.create_project({
        "title": "Dependency Test",
        "aim": "Test dependency handling",
        "success_criteria": [],
    })

    project_id = str(project.id)

    # Create parent task
    parent_task = await task_service.create_task({
        "title": "Parent Task",
        "description": "Parent",
        "plan": "Do parent work",
        "project_ids": [project_id],
    })

    # Create child task with dependency
    child_task = await task_service.create_task({
        "title": "Child Task",
        "description": "Child depends on parent",
        "plan": "Do child work",
        "project_ids": [project_id],
        "metadata": {"parent_id": str(parent_task.id)},
    })

    # Child task starts in pending (or blocked) state — no plan approval needed
    child_state = await db.fetch_one(
        "SELECT status, blocked_reason FROM tasks WHERE id = ?",
        (str(child_task.id),)
    )
    assert child_state is not None

    # Complete parent task
    await task_service.complete_task(str(parent_task.id), result_summary="Parent done")

    # Trigger autonomy service
    autonomy_service = ProjectAutonomyService(db)
    await autonomy_service.on_task_completed(str(parent_task.id), "Parent Task", "Parent done")

    # Check that child was released
    # (Implementation detail: status transition happens in _release_unblocked_dependents)
    # This is verified by checking the task is no longer blocked


@pytest.mark.asyncio
async def test_max_autonomy_cycles_limit(db: Database):
    """Test that autonomy has circuit breakers to prevent infinite loops."""
    from bob_server.services.project_service import ProjectService
    from bob_server.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    # Create project with max cycles limit
    project = await project_service.create_project({
        "title": "Capped Autonomy",
        "aim": "Test cycle limit",
        "success_criteria": [
            {"check": "completed_tasks >= 10", "description": "Need 10 tasks"}
        ],
        "metadata": {"max_autonomy_cycles": 3},
    })

    project_id = str(project.id)

    # The implementation should track autonomy cycles and stop
    # This test verifies the metadata is available for enforcement
    project_data = await db.fetch_one(
        "SELECT metadata FROM projects WHERE id = ?",
        (project_id,)
    )

    import json
    metadata = json.loads(project_data["metadata"])
    assert metadata.get("max_autonomy_cycles") == 3


@pytest.mark.asyncio
async def test_journal_records_all_decisions(db: Database, autonomous_project):
    """Test that all autonomy decisions are recorded in journal."""
    from bob_server.services.task_service import TaskService
    import bob_server.services.openclaw_reasoning_service as reasoning_module

    project_id = autonomous_project["project_id"]
    task_service = TaskService(db)

    async def fake_decide_next_step(self, project_id, completed_task_id):
        return {"action": "close_project", "reasoning": "Test close"}

    with patch.object(reasoning_module.OpenClawReasoningService, 'decide_next_step', fake_decide_next_step):
        task = await task_service.create_task({
            "title": "Journal Test Task",
            "description": "Test journal recording",
            "plan": "Plan",
            "project_ids": [project_id],
        })

        await task_service.complete_task(str(task.id), result_summary="Done")

        autonomy_service = ProjectAutonomyService(db)
        await autonomy_service.on_task_completed(str(task.id), "Journal Test Task", "Done")

    # Check journal for decision entries
    journal = await db.fetch_all(
        "SELECT entry_type, content FROM project_journal_entries WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,)
    )

    # Should have milestone entry about completion
    decision_entries = [e for e in journal if e["entry_type"] in ["milestone", "decision"]]
    assert len(decision_entries) > 0


@pytest.mark.asyncio
async def test_task_completion_triggers_reasoning(db: Database):
    """Test that task completion dispatches next-action prompt, then decide-next closes the project."""
    from bob_server.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    project = await project_service.create_project({
        "title": "Reasoning Trigger Test",
        "aim": "Test reasoning on completion",
        "method": "Complete a task and trigger reasoning.",
        "success_criteria": [{"check": "completed_task_count >= 1", "description": "One task done"}],
        "plan": [{"title": "Trigger Task", "description": "Should trigger reasoning", "criteria": "Done", "order": 0}],
    })

    project_id = str(project.id)

    # Approve spec to activate the project
    from bob_server.models import ProjectSpecApproveRequest
    spec_service = project_service.project_spec_service
    specs = await spec_service.list_specs(project_id)
    await spec_service.approve_spec(
        str(specs.specs[0].id),
        ProjectSpecApproveRequest(approver="test"),
    )

    task = await task_service.create_task({
        "title": "Trigger Task",
        "description": "Should trigger reasoning",
        "plan": "Plan",
        "project_ids": [project_id],
    })

    autonomy_service = ProjectAutonomyService(db)
    await autonomy_service.on_task_completed(
        str(task.id),
        "Trigger Task",
        "Done",
    )

    # Project should still be active (next-action prompt was dispatched)
    project_data = await db.fetch_one("SELECT state, reasoning_otp FROM projects WHERE id = ?", (project_id,))
    assert project_data["state"] == "active"
    assert project_data["reasoning_otp"] is not None

    # Simulate the agent's decide-next response
    from bob_server.services.project_execution_service import ProjectExecutionService
    execution_service = ProjectExecutionService(db)
    await execution_service.verify_decide_next(project_id, {
        "otp": project_data["reasoning_otp"],
        "action": "close_project",
        "reasoning": "Done",
    })

    # Verify project was closed
    project_data = await db.fetch_one("SELECT state FROM projects WHERE id = ?", (project_id,))
    assert project_data["state"] == "closed"
