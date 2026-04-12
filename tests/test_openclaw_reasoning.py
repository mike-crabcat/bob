"""Mocked contract-style tests for OpenClawReasoningService."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from cyborg.models import (
    JournalEntryType,
    PlanStep,
    ProjectCloseRequest,
    ProjectCreate,
    ProjectState,
    SuccessCriterion,
    TaskBlockRequest,
    TaskCreate,
)
from cyborg.services.openclaw_hook_service import OpenClawHookService
from cyborg.services.context_builder import ContextScope
from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService
from cyborg.database import Database


@pytest_asyncio.fixture
async def sample_project_with_data(db: Database):
    """Create a sample project with tasks for testing."""
    from cyborg.services.project_service import ProjectService
    from cyborg.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    # Create project with success criteria
    project = await project_service.create_project(ProjectCreate(
        title="Test Evaluation Project",
        aim="Test automated evaluation",
        method="Use OpenClaw reasoning",
        plan=[
            PlanStep(
                order=0,
                title="Complete primary task",
                description="Do the main work",
                criteria="Task is done"
            ),
            PlanStep(
                order=1,
                title="Verify results",
                description="Check that everything works",
                criteria="Tests pass"
            ),
        ],
        success_criteria=[
            SuccessCriterion(
                check="completed_tasks >= 2",
                description="Complete at least 2 tasks"
            ),
            SuccessCriterion(
                check="failed_tasks == 0",
                description="No failed tasks"
            ),
        ],
    ))

    project_id = str(project.id)

    # Create and complete tasks
    task1 = await task_service.create_task(TaskCreate(
        title="Primary Task",
        description="Main work",
        plan="Do it",
        project_ids=[project_id],
    ))

    task2 = await task_service.create_task(TaskCreate(
        title="Verification Task",
        description="Check results",
        plan="Verify",
        project_ids=[project_id],
    ))

    # Complete both tasks
    await task_service.complete_task(
        str(task1.id),
        result_summary="Task completed successfully"
    )

    await task_service.complete_task(
        str(task2.id),
        result_summary="All tests passed"
    )

    return {
        "project_id": project_id,
        "task1_id": str(task1.id),
        "task2_id": str(task2.id),
    }


@pytest.mark.asyncio
async def test_evaluate_success_criteria_with_mocked_openclaw(db: Database, sample_project_with_data):
    """Test evaluation with mocked OpenClaw response."""
    project_id = sample_project_with_data["project_id"]

    reasoning_service = OpenClawReasoningService(db)

    # Mock the OpenClaw gateway call at the class level
    mock_response = {
        "content": '''{
  "all_met": true,
  "met_criteria": ["Complete at least 2 tasks", "No failed tasks"],
  "unmet_criteria": [],
  "reasoning": "Both tasks have been completed successfully with no failures. All success criteria have been satisfied."
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        result = await reasoning_service.evaluate_success_criteria(project_id)

        # Verify the call was made
        mock_gateway.assert_called_once()
        call_args = mock_gateway.call_args

        # Check the method and params
        assert call_args.kwargs["method"] == "agent"
        params = call_args.kwargs["params"]
        assert params["deliver"] == False
        assert params["sessionKey"].startswith("cyborg:reasoning:")
        assert "criteria-evaluation" in params["sessionKey"]
        assert "Test Evaluation Project" in params["message"]

        # Verify the parsed response
        assert result["all_met"] == True
        assert len(result["met_criteria"]) == 2
        assert len(result["unmet_criteria"]) == 0
        assert "reasoning" in result


@pytest.mark.asyncio
async def test_evaluate_success_criteria_unmet(db: Database):
    """Test evaluation when criteria are not met."""
    from cyborg.services.project_service import ProjectService
    from cyborg.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    # Create project
    project = await project_service.create_project(ProjectCreate(
        title="Incomplete Project",
        aim="Test unmet criteria",
        success_criteria=[
            SuccessCriterion(
                check="completed_tasks >= 5",
                description="Need 5 completed tasks"
            )
        ],
    ))

    project_id = str(project.id)

    # Create only 2 tasks
    for i in range(2):
        task = await task_service.create_task(TaskCreate(
            title=f"Task {i}",
            description=f"Task number {i}",
            plan="Do it",
            project_ids=[project_id],
        ))
        await task_service.complete_task(str(task.id))

    reasoning_service = OpenClawReasoningService(db)

    # Mock OpenClaw response indicating unmet criteria
    mock_response = {
        "content": '''{
  "all_met": false,
  "met_criteria": [],
  "unmet_criteria": ["Need 5 completed tasks"],
  "reasoning": "Only 2 tasks have been completed, but the success criteria requires 5 completed tasks. More work is needed."
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.evaluate_success_criteria(project_id)

        assert result["all_met"] == False
        assert len(result["unmet_criteria"]) == 1
        assert "5 completed tasks" in result["unmet_criteria"][0]


@pytest.mark.asyncio
async def test_generate_follow_up_tasks(db: Database):
    """Test generating follow-up tasks for unmet criteria."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)

    project = await project_service.create_project(ProjectCreate(
        title="Follow-up Test",
        aim="Test follow-up generation",
        success_criteria=[],
    ))

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    # Mock OpenClaw response with task suggestions
    mock_response = {
        "content": '''{
  "tasks": [
    {
      "title": "Complete remaining work",
      "description": "Finish the incomplete tasks",
      "plan": "1. Identify remaining tasks\\n2. Complete them",
      "priority": "high"
    }
  ]
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        unmet_criteria = ["Need more completed tasks"]
        result = await reasoning_service.generate_follow_up_tasks(project_id, unmet_criteria)

        assert len(result) == 1
        assert result[0]["title"] == "Complete remaining work"
        assert result[0]["priority"] == "high"


@pytest.mark.asyncio
async def test_refine_project_strategy(db: Database, sample_project_with_data):
    """Test strategy refinement reasoning."""
    project_id = sample_project_with_data["project_id"]
    trigger_task_id = sample_project_with_data["task1_id"]

    reasoning_service = OpenClawReasoningService(db)

    # Mock refinement response
    mock_response = {
        "content": '''{
  "should_refine": false,
  "reasoning": "Project is progressing well. No refinement needed at this time.",
  "suggested_changes": [],
  "new_priorities": {},
  "risks_identified": []
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.refine_project_strategy(project_id, trigger_task_id)

        assert result["should_refine"] == False
        assert "reasoning" in result


@pytest.mark.asyncio
async def test_refine_project_strategy_with_changes(db: Database):
    """Test strategy refinement when changes are suggested."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)

    project = await project_service.create_project(ProjectCreate(
        title="Needs Refinement",
        aim="Test refinement with changes",
        success_criteria=[],
    ))

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    # Mock response suggesting changes
    mock_response = {
        "content": '''{
  "should_refine": true,
  "reasoning": "Current approach is not optimal. A different strategy would be more effective.",
  "suggested_changes": [
    {
      "type": "change_approach",
      "description": "Switch from message queue to synchronous communication"
    },
    {
      "type": "add_task",
      "description": "Implement performance testing"
    }
  ],
  "new_priorities": {},
  "risks_identified": ["Performance may degrade without testing"]
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        trigger_task_id = "some-task-id"
        result = await reasoning_service.refine_project_strategy(project_id, trigger_task_id)

        assert result["should_refine"] == True
        assert len(result["suggested_changes"]) == 2
        assert len(result["risks_identified"]) == 1


@pytest.mark.asyncio
async def test_extract_learnings(db: Database):
    """Test learning extraction from completed project."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)

    project = await project_service.create_project(ProjectCreate(
        title="Completed for Learning",
        aim="Extract lessons",
        success_criteria=[],
    ))

    # Close the project
    await project_service.close_project(
        str(project.id),
        ProjectCloseRequest(conclusion="Project completed successfully"),
    )

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    # Mock learning response
    mock_response = {
        "content": '''{
  "insights": [
    {
      "category": "planning",
      "lesson": "Early proof-of-concept testing prevents architecture pivots",
      "applicability": "All projects introducing new technologies",
      "impact": "positive"
    },
    {
      "category": "estimation",
      "lesson": "Migration projects take 30% longer than estimated",
      "applicability": "Migration and rewrite projects",
      "impact": "neutral"
    }
  ],
  "success_patterns": ["Incremental rollout reduced risk"],
  "failure_patterns": ["Choosing technology without performance validation"],
  "recommendations": ["Always benchmark new technologies before commitment"]
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.extract_learnings(project_id)

        assert len(result) == 2
        assert result[0]["category"] == "planning"
        assert result[0]["impact"] == "positive"


@pytest.mark.asyncio
async def test_generate_task_plan(db: Database):
    """Test task-level plan generation."""
    from cyborg.services.task_service import TaskService
    from cyborg.services.project_service import ProjectService
    from cyborg.models import ProjectCreate

    task_service = TaskService(db)
    project_service = ProjectService(db)

    # Create a project for the task (tasks now require projects)
    project = await project_service.create_project(ProjectCreate(
        title="Test Project",
        aim="Test project for task plan generation",
    ))
    project_id = str(project.id)

    task = await task_service.create_task(TaskCreate(
        title="Complex Task",
        description="Needs detailed planning",
        plan="Initial plan",
        project_ids=[project_id],
    ))

    task_id = str(task.id)

    reasoning_service = OpenClawReasoningService(db)

    # Mock task planning response
    mock_response = {
        "content": """1. Analyze requirements thoroughly
2. Design solution approach
3. Implement core functionality
4. Test and validate
5. Document and deploy"""
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.generate_task_plan(task_id)

        assert isinstance(result, str)
        assert "Analyze requirements" in result
        assert len(result.split("\n")) == 5


@pytest.mark.asyncio
async def test_analyze_project_health(db: Database):
    """Test project health analysis."""
    from cyborg.services.project_service import ProjectService
    from cyborg.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    project = await project_service.create_project(ProjectCreate(
        title="At Risk Project",
        aim="Health check test",
        success_criteria=[],
    ))

    project_id = str(project.id)

    # Create some blocked tasks
    for i in range(2):
        task = await task_service.create_task(TaskCreate(
            title=f"Blocked Task {i}",
            description=f"Task {i}",
            plan="Do it",
            project_ids=[project_id],
        ))
        # Block the task
        await task_service.block_task(
            str(task.id),
            TaskBlockRequest(
                reason="Waiting for dependency",
                resume_instructions="Complete parent task first",
            ),
        )

    reasoning_service = OpenClawReasoningService(db)

    # Mock health analysis response
    mock_response = {
        "content": '''{
  "health_status": "at_risk",
  "risk_level": "high",
  "blockers": [
    {
      "task": "Blocked Task 0",
      "severity": "medium",
      "recommendation": "Resolve dependencies to unblock tasks"
    }
  ],
  "schedule_risk": "at_risk",
  "recommendations": [
    {
      "priority": "high",
      "action": "Address blocked tasks immediately"
    }
  ],
  "escalation_required": true,
  "reasoning": "Multiple blocked tasks indicate the project has stalled."
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.analyze_project_health(project_id)

        assert result["health_status"] == "at_risk"
        assert result["risk_level"] == "high"
        assert result["escalation_required"] == True


@pytest.mark.asyncio
async def test_generate_project_plan(db: Database):
    """Test project plan generation from objective."""
    reasoning_service = OpenClawReasoningService(db)

    # Mock plan generation response
    mock_response = {
        "content": '''{
  "steps": [
    {
      "order": 0,
      "title": "Design database schema",
      "description": "Design data models and relationships",
      "criteria": "ERD approved"
    },
    {
      "order": 1,
      "title": "Implement API endpoints",
      "description": "Build REST API for core operations",
      "criteria": "All endpoints tested"
    }
  ]
}'''
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.generate_project_plan(
            aim="Build a customer portal",
            method="Use FastAPI and PostgreSQL",
            success_criteria=["Portal handles 1000 users", "Users can view orders"]
        )

        assert len(result) == 2
        assert result[0]["title"] == "Design database schema"
        assert result[1]["order"] == 1


@pytest.mark.asyncio
async def test_openclaw_unavailable_handling(db: Database):
    """Test handling when OpenClaw gateway is unavailable."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)
    project = await project_service.create_project(ProjectCreate(
        title="Test Failure",
        aim="Test error handling",
        success_criteria=[],
    ))

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    # Mock gateway failure
    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(side_effect=Exception("Gateway unavailable"))
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await reasoning_service.evaluate_success_criteria(project_id)

        assert "OpenClaw reasoning failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_malformed_json_response(db: Database):
    """Test handling of malformed JSON from OpenClaw."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)
    project = await project_service.create_project(ProjectCreate(
        title="JSON Test",
        aim="Test JSON parsing",
        success_criteria=[],
    ))

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    # Mock response with invalid JSON
    mock_response = {
        "content": "This is not valid JSON {broken"
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ):
        result = await reasoning_service.evaluate_success_criteria(project_id)

        # Should return safe default rather than crash
        assert result["all_met"] == False
        assert "Failed to parse" in result["reasoning"]


@pytest.mark.asyncio
async def test_idempotency_key_included(db: Database):
    """Test that idempotency key is included in gateway calls."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)
    project = await project_service.create_project(ProjectCreate(
        title="Idempotency Test",
        aim="Test idempotency",
        success_criteria=[],
    ))

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        await reasoning_service.evaluate_success_criteria(project_id)

        # Check idempotency key was included
        call_params = mock_gateway.call_args[1]["params"]
        assert "idempotencyKey" in call_params
        assert len(call_params["idempotencyKey"]) > 0


@pytest.mark.asyncio
async def test_response_format_json_hint(db: Database):
    """Test that JSON response format is requested."""
    from cyborg.services.project_service import ProjectService

    project_service = ProjectService(db)
    project = await project_service.create_project(ProjectCreate(
        title="Format Test",
        aim="Test response format",
        success_criteria=[],
    ))

    project_id = str(project.id)

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        await reasoning_service.evaluate_success_criteria(project_id)

        # Check message includes JSON format hint
        call_params = mock_gateway.call_args[1]["params"]
        message = call_params["message"]
        assert "valid JSON only" in message
        assert "No markdown" in message


@pytest.mark.asyncio
async def test_context_builder_integration(db: Database, sample_project_with_data):
    """Test that context builder is called correctly."""
    project_id = sample_project_with_data["project_id"]

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
    }

    # Patch both gateway and context builder
    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ), patch.object(
        reasoning_service.context_builder,
        'build_project_context',
        new=AsyncMock(return_value={
            "core": {
                "project": {"title": "Test", "aim": "Test aim"},
                "success_criteria": {"criteria": []},
            },
            "tasks": {"summary": {"total": 0, "completed": 0, "failed": 0, "active": 0}},
            "journal": {"entries": []},
        })
    ) as mock_context:
        await reasoning_service.evaluate_success_criteria(project_id)

        # Verify context builder was called with correct scope
        mock_context.assert_called_once()
        call_args = mock_context.call_args
        assert call_args.kwargs["project_id"] == project_id
        assert call_args.kwargs["scope"] == ContextScope.STANDARD
        assert call_args.kwargs["focus_reasoning"] == "evaluation"


@pytest.mark.asyncio
async def test_fresh_session_key_per_call(db: Database, sample_project_with_data):
    """Each reasoning call should get a unique session key."""
    project_id = sample_project_with_data["project_id"]

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        await reasoning_service.evaluate_success_criteria(project_id)
        await reasoning_service.evaluate_success_criteria(project_id)

        assert mock_gateway.call_count == 2
        call1_params = mock_gateway.call_args_list[0].kwargs["params"]
        call2_params = mock_gateway.call_args_list[1].kwargs["params"]

        session_key_1 = call1_params["sessionKey"]
        session_key_2 = call2_params["sessionKey"]

        assert session_key_1 != session_key_2
        assert session_key_1.startswith("cyborg:reasoning:")
        assert session_key_2.startswith("cyborg:reasoning:")


@pytest.mark.asyncio
async def test_session_key_includes_reasoning_type(db: Database, sample_project_with_data):
    """Session key should encode the reasoning type."""
    project_id = sample_project_with_data["project_id"]

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        await reasoning_service.evaluate_success_criteria(project_id)

        params = mock_gateway.call_args.kwargs["params"]
        assert "criteria-evaluation" in params["sessionKey"]


@pytest.mark.asyncio
async def test_session_key_override(db: Database, sample_project_with_data):
    """Explicit session_key param should be used as-is."""
    project_id = sample_project_with_data["project_id"]

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        # Call _call_openclaw directly with an explicit session_key
        await reasoning_service._call_openclaw(
            prompt="test",
            response_format="text",
            session_key="my-custom-session",
        )

        params = mock_gateway.call_args.kwargs["params"]
        assert params["sessionKey"] == "my-custom-session"


@pytest.mark.asyncio
async def test_upstream_context_in_evaluation_prompt(db: Database):
    """Evaluation prompt should mention upstream task results when they exist."""
    from cyborg.services.project_service import ProjectService
    from cyborg.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    project = await project_service.create_project(ProjectCreate(
        title="Upstream Context Test",
        aim="Test upstream context in prompts",
        success_criteria=[
            SuccessCriterion(check="completed_tasks >= 1", description="One task done"),
        ],
    ))
    project_id = str(project.id)

    # Create parent task and complete it
    parent = await task_service.create_task(TaskCreate(
        title="Parent Research Task",
        description="Do the research",
        plan="Research",
        project_ids=[project_id],
    ))
    await task_service.complete_task(str(parent.id), result_summary="Found key insights about the domain")

    # Add a file to the parent task
    await db.execute(
        """INSERT INTO task_files (id, task_id, project_id, filename, relative_path, purpose, content_type, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (str(__import__('uuid').uuid4()), str(parent.id), project_id, "report.md",
         f"tasks/{str(parent.id).replace('-','')[:8]}/report.md", "result", "text/markdown"),
    )

    # Create child task with parent reference
    child = await task_service.create_task(TaskCreate(
        title="Child Analysis Task",
        description="Analyze the research",
        plan="Analyze",
        project_ids=[project_id],
    ))
    # Set parent_id via direct DB update
    await db.execute(
        "UPDATE tasks SET parent_id = ? WHERE id = ?",
        (str(parent.id), str(child.id)),
    )

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"all_met": true, "met_criteria": ["One task done"], "unmet_criteria": [], "reasoning": "OK"}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        await reasoning_service.evaluate_success_criteria(project_id)

        params = mock_gateway.call_args.kwargs["params"]
        message = params["message"]

        assert "Upstream Task Results" in message
        assert "Parent Research Task" in message
        assert "Found key insights" in message


@pytest.mark.asyncio
async def test_upstream_context_in_refinement_prompt(db: Database):
    """Refinement prompt should include upstream context."""
    from cyborg.services.project_service import ProjectService
    from cyborg.services.task_service import TaskService

    project_service = ProjectService(db)
    task_service = TaskService(db)

    project = await project_service.create_project(ProjectCreate(
        title="Refinement Upstream Test",
        aim="Test upstream in refinement",
        success_criteria=[],
    ))
    project_id = str(project.id)

    parent = await task_service.create_task(TaskCreate(
        title="Upstream Data Task",
        description="Collect data",
        plan="Collect",
        project_ids=[project_id],
    ))
    await task_service.complete_task(str(parent.id), result_summary="Data collected: 500 records")

    child = await task_service.create_task(TaskCreate(
        title="Downstream Analysis",
        description="Analyze collected data",
        plan="Analyze",
        project_ids=[project_id],
    ))
    await db.execute(
        "UPDATE tasks SET parent_id = ? WHERE id = ?",
        (str(parent.id), str(child.id)),
    )

    reasoning_service = OpenClawReasoningService(db)

    mock_response = {
        "content": '{"should_refine": false, "reasoning": "OK", "suggested_changes": [], "new_priorities": {}, "risks_identified": []}'
    }

    with patch.object(
        OpenClawHookService,
        '_send_gateway_request',
        new=AsyncMock(return_value=mock_response)
    ) as mock_gateway:
        await reasoning_service.refine_project_strategy(project_id, str(child.id))

        params = mock_gateway.call_args.kwargs["params"]
        message = params["message"]

        assert "Upstream Task Results" in message
        assert "Upstream Data Task" in message
        assert "500 records" in message
