"""End-to-end autonomy tests using mock LLM service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from cyborg_server.database import Database
from cyborg_core.models import SuccessCriterion, TaskStatus
from cyborg_server.services.base import utcnow
from cyborg_server.services.project_execution_service import ProjectExecutionService
from cyborg_server.services.project_autonomy_service import ProjectAutonomyService
from tests.mocks.mock_llm_service import MockLLMReasoningService


@pytest_asyncio.fixture
async def test_db():
    """Create a fresh test database for each test."""
    db_path = Path("/tmp/cyborg-e2e-test.db")
    if db_path.exists():
        db_path.unlink()

    db = Database(db_path=db_path, schema_dir=Path(__file__).parent.parent / "cyborg" / "schemas", pool_size=1)
    await db.connect()
    await db.apply_migrations()

    yield db

    await db.close()
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def mock_llm(test_db: Database) -> MockLLMReasoningService:
    """Create mock LLM service."""
    return MockLLMReasoningService(test_db)


@pytest_asyncio.fixture
async def execution_service(test_db: Database, mock_llm: MockLLMReasoningService) -> ProjectExecutionService:
    """Create execution service with mock LLM."""
    service = ProjectExecutionService(test_db)
    service._reasoning_service = mock_llm
    return service


async def create_test_project(
    db: Database,
    title: str = "Test Project",
    aim: str = "Test aim",
    method: str = "Test method",
    success_criteria: list[dict] | None = None,
) -> dict:
    """Helper to create a test project with spec."""
    project_id = str(uuid4())

    # Create project
    import json
    await db.execute(
        """
        INSERT INTO projects (id, title, aim, method, state, success_criteria, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, title, aim, method, "planning", json.dumps(success_criteria or []), utcnow().isoformat()),
    )

    # Create spec
    spec_id = str(uuid4())
    await db.execute(
        """
        INSERT INTO project_specs (id, project_id, version_number, status, aim, method, success_criteria, created_at, is_current)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, 1)
        """,
        (spec_id, project_id, "approved", aim, method, json.dumps(success_criteria or []), utcnow().isoformat()),
    )

    # Update project with current spec
    await db.execute(
        "UPDATE projects SET current_spec_id = ?, state = ? WHERE id = ?",
        (spec_id, "active", project_id),
    )

    return await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))


async def create_test_task(
    db: Database,
    project_id: str,
    title: str = "Test Task",
    status: str = TaskStatus.ACTIVE.value,
) -> dict:
    """Helper to create a test task."""
    task_id = str(uuid4())

    await db.execute(
        """
        INSERT INTO tasks (id, title, plan, status, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, title, "Test plan", status, "medium", utcnow().isoformat(), utcnow().isoformat()),
    )

    # Link to project
    await db.execute(
        "INSERT INTO project_tasks (project_id, task_id) VALUES (?, ?)",
        (project_id, task_id),
    )

    return await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))


async def complete_task(db: Database, task_id: str, result_summary: str = "Task completed"):
    """Helper to complete a task."""
    await db.execute(
        """
        UPDATE tasks
        SET status = ?, result = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (TaskStatus.COMPLETED.value, result_summary, utcnow().isoformat(), utcnow().isoformat(), task_id),
    )


@pytest.mark.asyncio
async def test_full_autonomy_loop_project_completion(test_db: Database, mock_llm: MockLLMReasoningService, execution_service: ProjectExecutionService):
    """Test complete autonomy flow: create project → complete tasks → project auto-closes."""

    # Create project with success criteria: completed_tasks >= 2
    project = await create_test_project(
        test_db,
        title="Auto-Complete Test",
        aim="Test autonomous completion",
        method="Execute tasks",
        success_criteria=[
            {"check": "completed_task_count >= 2", "description": "Complete at least 2 tasks"},
        ],
    )
    project_id = project["id"]

    # Create two tasks
    task1 = await create_test_task(test_db, project_id, "Task 1")
    task2 = await create_test_task(test_db, project_id, "Task 2")

    # Complete both tasks
    await complete_task(test_db, task1["id"])
    await complete_task(test_db, task2["id"])

    # Evaluate - should close since both tasks complete (mock LLM evaluates deterministically)
    result = await execution_service.evaluate_and_complete(project_id)

    # Verify project closed
    updated_project = await test_db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    assert updated_project["state"] == "closed"
    assert updated_project["closed_at"] is not None
    assert result is not None


@pytest.mark.asyncio
async def test_evaluate_does_not_close_when_criteria_unmet(test_db: Database, mock_llm: MockLLMReasoningService, execution_service: ProjectExecutionService):
    """Test that evaluate does not close when success criteria aren't met."""

    # Create project with criteria: completed_tasks >= 5
    project = await create_test_project(
        test_db,
        title="Criteria Unmet Test",
        aim="Test criteria not met",
        method="Execute tasks",
        success_criteria=[
            {"check": "completed_task_count >= 5", "description": "Complete at least 5 tasks"},
        ],
    )
    project_id = project["id"]

    # Create only 2 tasks and complete them
    task1 = await create_test_task(test_db, project_id, "Task 1")
    task2 = await create_test_task(test_db, project_id, "Task 2")
    await complete_task(test_db, task1["id"])
    await complete_task(test_db, task2["id"])

    # Evaluate - should NOT close (criteria not met)
    result = await execution_service.evaluate_and_complete(project_id)

    # Project should still be active
    updated_project = await test_db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    assert updated_project["state"] == "active"
    assert result is None

    # Verify mock LLM was called
    assert mock_llm.call_count > 0


@pytest.mark.asyncio
async def test_concurrent_projects_evaluate_independently(test_db: Database, mock_llm: MockLLMReasoningService, execution_service: ProjectExecutionService):
    """Test that multiple projects evaluate independently."""

    # Create 3 projects with different criteria
    project1 = await create_test_project(
        test_db,
        title="Project 1",
        aim="Test project 1",
        method="Execute",
        success_criteria=[{"check": "completed_task_count >= 1", "description": "Complete 1 task"}],
    )

    project2 = await create_test_project(
        test_db,
        title="Project 2",
        aim="Test project 2",
        method="Execute",
        success_criteria=[{"check": "completed_task_count >= 2", "description": "Complete 2 tasks"}],
    )

    project3 = await create_test_project(
        test_db,
        title="Project 3",
        aim="Test project 3",
        method="Execute",
        success_criteria=[{"check": "completed_task_count >= 3", "description": "Complete 3 tasks"}],
    )

    # Create tasks for each project
    task1 = await create_test_task(test_db, project1["id"], "P1 Task 1")
    await complete_task(test_db, task1["id"])

    task2a = await create_test_task(test_db, project2["id"], "P2 Task 1")
    task2b = await create_test_task(test_db, project2["id"], "P2 Task 2")
    await complete_task(test_db, task2a["id"])
    await complete_task(test_db, task2b["id"])

    task3a = await create_test_task(test_db, project3["id"], "P3 Task 1")
    await complete_task(test_db, task3a["id"])

    # Evaluate all projects
    result1 = await execution_service.evaluate_and_complete(project1["id"])
    result2 = await execution_service.evaluate_and_complete(project2["id"])
    result3 = await execution_service.evaluate_and_complete(project3["id"])

    # Check states
    p1_state = await test_db.fetch_one("SELECT state FROM projects WHERE id = ?", (project1["id"],))
    p2_state = await test_db.fetch_one("SELECT state FROM projects WHERE id = ?", (project2["id"],))
    p3_state = await test_db.fetch_one("SELECT state FROM projects WHERE id = ?", (project3["id"],))

    # Project 1: 1/1 tasks done → should close
    assert p1_state["state"] == "closed"

    # Project 2: 2/2 tasks done → should close
    assert p2_state["state"] == "closed"

    # Project 3: 1/3 tasks done → should stay active
    assert p3_state["state"] == "active"


@pytest.mark.asyncio
async def test_mock_llm_health_analysis(test_db: Database, mock_llm: MockLLMReasoningService):
    """Test mock LLM health analysis functionality."""

    project = await create_test_project(test_db)

    # Create some tasks
    await create_test_task(test_db, project["id"], "Task 1", TaskStatus.COMPLETED.value)
    await create_test_task(test_db, project["id"], "Task 2", TaskStatus.FAILED.value)
    await create_test_task(test_db, project["id"], "Task 3", TaskStatus.BLOCKED.value)

    # Get health analysis
    health = await mock_llm.analyze_project_health(project["id"])

    assert "health_score" in health
    assert "risk_level" in health
    assert "indicators" in health
    assert health["indicators"]["total_tasks"] == 3
    assert health["indicators"]["failed_tasks"] == 1
    assert health["indicators"]["blocked_tasks"] == 1


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "-s"])
