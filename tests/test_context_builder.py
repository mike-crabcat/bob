"""Tests for ContextBuilder service."""

import pytest
import pytest_asyncio
from cyborg.models import PlanStep, ProjectCreate, SuccessCriterion, TaskCreate
from cyborg.services.context_builder import ContextBuilder, ContextScope
from cyborg.database import Database


@pytest_asyncio.fixture
async def sample_project(db: Database):
    """Create a sample project with tasks and journal entries."""
    from cyborg.services.project_service import ProjectService
    from cyborg.services.task_service import TaskService
    import json
    from uuid import uuid4

    project_service = ProjectService(db)
    task_service = TaskService(db)

    # Create project
    project = await project_service.create_project(ProjectCreate(
        title="Test Project",
        aim="Test autonomous execution",
        method="Use OpenClaw for reasoning",
        plan=[
            PlanStep(order=0, title="Step 1", description="First step", criteria="Done"),
            PlanStep(order=1, title="Step 2", description="Second step", criteria="Done"),
        ],
        success_criteria=[
            SuccessCriterion(check="completed_tasks >= 2", description="Complete 2 tasks"),
        ],
        auto_execute=True,
    ))

    project_id = str(project.id)

    # Create tasks
    task1 = await task_service.create_task(TaskCreate(
        title="Test Task 1",
        description="First test task",
        plan="Do the thing",
        project_ids=[project_id],
    ))

    task2 = await task_service.create_task(TaskCreate(
        title="Test Task 2",
        description="Second test task",
        plan="Do another thing",
        project_ids=[project_id],
    ))

    # Complete one task
    await task_service.complete_task(
        str(task1.id),
        result_summary="Task completed successfully"
    )

    # Add journal entries
    await db.execute(
        """
        INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        """,
        (str(uuid4()), project_id, "milestone", "Project started")
    )

    await db.execute(
        """
        INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at)
        VALUES (?, ?, ?, ?, datetime('now', '-1 day'))
        """,
        (str(uuid4()), project_id, "decision", "Decided to use OpenClaw")
    )

    # Create 30 more journal entries to test summarization
    for i in range(30):
        await db.execute(
            """
            INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at)
            VALUES (?, ?, ?, ?, datetime('now', '-%d days'))
            """ % (i + 2),
            (str(uuid4()), project_id, "note", f"Journal entry {i}")
        )

    return {
        "project_id": project_id,
        "task1_id": str(task1.id),
        "task2_id": str(task2.id),
    }


@pytest.mark.asyncio
async def test_build_minimal_context(db: Database, sample_project):
    """Test minimal context assembly."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.MINIMAL,
    )

    assert context["scope"] == ContextScope.MINIMAL
    assert "core" in context
    assert "tasks" in context
    assert "journal" in context
    assert "temporal" in context
    assert "generated_at" in context

    # Minimal should have fewer journal entries
    assert len(context["journal"]["entries"]) <= 10
    assert context["journal"]["total_entries"] == 33

    # Should estimate reasonable token count
    assert context["metadata"]["total_estimated_tokens"] < 5000


@pytest.mark.asyncio
async def test_build_standard_context(db: Database, sample_project):
    """Test standard context assembly."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.STANDARD,
    )

    assert context["scope"] == ContextScope.STANDARD
    assert context["core"]["project"]["title"] == "Test Project"
    assert context["core"]["project"]["aim"] == "Test autonomous execution"

    # Standard should include milestones/decisions
    entry_types = {e["entry_type"] for e in context["journal"]["entries"]}
    assert "milestone" in entry_types
    assert "decision" in entry_types

    # Should have plan and criteria
    assert len(context["core"]["plan"]["steps"]) == 2
    assert context["core"]["success_criteria"]["total_count"] == 1

    # Reasonable token count
    assert context["metadata"]["total_estimated_tokens"] < 15000


@pytest.mark.asyncio
async def test_build_comprehensive_context(db: Database, sample_project):
    """Test comprehensive context assembly."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.COMPREHENSIVE,
    )

    assert context["scope"] == ContextScope.COMPREHENSIVE

    # Comprehensive should preserve the full journal for projects of this size
    assert context["journal"]["summarized"] is False

    # Should have more task detail
    assert "tasks" in context
    assert context["tasks"]["summary"]["total"] == 2
    assert context["tasks"]["summary"]["completed"] == 1


@pytest.mark.asyncio
async def test_build_full_context(db: Database, sample_project):
    """Test full context assembly."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.FULL,
    )

    assert context["scope"] == ContextScope.FULL

    # Full should have all journal entries
    assert context["journal"]["total_entries"] == 33
    # May be summarized if > 50
    assert "entries" in context["journal"]


@pytest.mark.asyncio
async def test_context_filters_by_focus_evaluation(db: Database, sample_project):
    """Test context includes relevant items based on evaluation focus."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.STANDARD,
        focus_reasoning="evaluation",
    )

    # Evaluation focus should include task state
    assert context["tasks"]["summary"]["completed"] == 1
    assert context["tasks"]["summary"]["total"] == 2


@pytest.mark.asyncio
async def test_context_filters_by_focus_refinement(db: Database, sample_project):
    """Test context includes relevant items based on refinement focus."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    # Create a parent-child relationship to test refinement focus
    from cyborg.services.task_service import TaskService
    task_service = TaskService(db)

    child_task = await task_service.create_task({
        "title": "Child Task",
        "description": "Depends on first task",
        "plan": "Do child work",
        "project_ids": [project_id],
        "metadata": {"parent_id": sample_project["task1_id"]},
    })

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.STANDARD,
        focus_reasoning="refinement",
    )

    # Should include tasks for dependency analysis
    assert context["tasks"]["summary"]["total"] >= 2


@pytest.mark.asyncio
async def test_task_summary_accuracy(db: Database, sample_project):
    """Test task summary is accurate."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.STANDARD,
    )

    summary = context["tasks"]["summary"]
    assert summary["total"] == 2
    assert summary["completed"] == 1
    assert summary["pending"] == 1  # Second task is still pending
    assert summary["active"] == 0
    assert summary["failed"] == 0


@pytest.mark.asyncio
async def test_temporal_context(db: Database, sample_project):
    """Test temporal context is included."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.STANDARD,
    )

    temporal = context["temporal"]
    assert "current_timestamp" in temporal
    assert "project_age_days" in temporal
    assert "upcoming_events" in temporal

    # Project was just created, so age should be 0 or 1
    assert temporal["project_age_days"] >= 0


@pytest.mark.asyncio
async def test_recent_task_results(db: Database, sample_project):
    """Test recent completed task results are included."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.STANDARD,
    )

    results = context["tasks"]["recent_results"]
    assert len(results) >= 1
    assert results[0]["result"] == "Task completed successfully"


@pytest.mark.asyncio
async def test_token_estimation(db: Database, sample_project):
    """Test token estimation is reasonable."""
    builder = ContextBuilder(db)
    project_id = sample_project["project_id"]

    # Minimal scope
    context = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.MINIMAL,
    )
    assert context["metadata"]["total_estimated_tokens"] > 0
    assert context["metadata"]["total_estimated_tokens"] < 5000

    # Full scope should have more tokens
    context_full = await builder.build_project_context(
        project_id=project_id,
        scope=ContextScope.FULL,
    )
    assert context_full["metadata"]["total_estimated_tokens"] >= context["metadata"]["total_estimated_tokens"]


@pytest.mark.asyncio
async def test_nonexistent_project(db: Database):
    """Test handling of nonexistent project."""
    builder = ContextBuilder(db)

    context = await builder.build_project_context(
        project_id="nonexistent-id",
        scope=ContextScope.STANDARD,
    )

    # Should return empty context rather than error
    assert context["core"]["project"] == {}


@pytest.mark.asyncio
async def test_plan_summarization(db: Database):
    """Test that long plans are summarized."""
    from cyborg.services.project_service import ProjectService
    import json

    project_service = ProjectService(db)

    # Create project with long plan (> 10 steps)
    long_plan = [
        {"order": i, "title": f"Step {i}", "description": f"Description {i}", "criteria": f"Done {i}"}
        for i in range(15)
    ]

    project = await project_service.create_project({
        "title": "Long Plan Project",
        "aim": "Test plan summarization",
        "plan": long_plan,
        "success_criteria": [],
    })

    builder = ContextBuilder(db)
    context = await builder.build_project_context(
        project_id=str(project.id),
        scope=ContextScope.STANDARD,
    )

    # Plan should be summarized
    steps = context["core"]["plan"]["steps"]
    assert len(steps) < 15  # Should be summarized
    assert any(s.get("summary") for s in steps)  # At least one is a summary entry


@pytest.mark.asyncio
async def test_duration_calculation(db: Database):
    """Test project duration calculation."""
    from cyborg.models import ProjectSpecApproveRequest
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)

    project = await project_service.create_project({
        "title": "Duration Test",
        "aim": "Test duration",
        "method": "Start the project and inspect the calculated duration.",
        "success_criteria": [
            {"check": "completed_task_count >= 1", "description": "One task completed"}
        ],
        "plan": [
            {"title": "Complete task", "description": "Do the work", "criteria": "Done", "order": 0},
        ],
    })

    specs = await project_service.project_spec_service.list_specs(str(project.id))
    await project_service.project_spec_service.approve_spec(
        str(specs.specs[0].id),
        ProjectSpecApproveRequest(approver="Test"),
    )

    # Spec approval auto-triggers execution, project is already active

    builder = ContextBuilder(db)
    context = await builder.build_project_context(
        project_id=str(project.id),
        scope=ContextScope.MINIMAL,
    )

    # Duration should be calculated since start
    duration = context["core"]["project"]["duration_days"]
    assert duration is not None
    assert duration >= 0
