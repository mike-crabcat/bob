"""Tests for the prompt_history helper and its integration into services."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from cyborg.database import Database
from cyborg.services.prompt_history import (
    PROMPT_CATEGORIES,
    estimate_token_count,
    log_prompt,
)


# ---------------------------------------------------------------------------
# Unit tests for the helper module
# ---------------------------------------------------------------------------

class TestEstimateTokenCount:
    def test_empty_string(self):
        assert estimate_token_count("") == 0

    def test_short_string(self):
        # 4 characters → 1 token
        assert estimate_token_count("abcd") == 1

    def test_rounds_down(self):
        # 7 characters → 1 token (7 // 4 == 1)
        assert estimate_token_count("abcdefg") == 1

    def test_longer_text(self):
        text = "a" * 100
        assert estimate_token_count(text) == 25


class TestLogPrompt:
    @pytest.mark.asyncio
    async def test_inserts_row(self, db: Database):
        await log_prompt(
            db,
            category="plan_generation",
            prompt_text="Generate a project plan for building a portal.",
            project_id="proj-1",
            task_id="task-1",
            session_key="sess-1",
        )

        rows = await db.fetch_all("SELECT * FROM prompt_history")
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["category"] == "plan_generation"
        assert row["prompt_text"] == "Generate a project plan for building a portal."
        assert row["project_id"] == "proj-1"
        assert row["task_id"] == "task-1"
        assert row["session_key"] == "sess-1"
        assert row["token_count_estimate"] == estimate_token_count(
            "Generate a project plan for building a portal."
        )
        assert row["id"] is not None

    @pytest.mark.asyncio
    async def test_all_valid_categories(self, db: Database):
        for cat in PROMPT_CATEGORIES:
            await log_prompt(db, category=cat, prompt_text=f"prompt for {cat}")

        rows = await db.fetch_all("SELECT * FROM prompt_history ORDER BY category")
        assert len(rows) == len(PROMPT_CATEGORIES)
        logged_cats = {r["category"] for r in rows}
        assert logged_cats == PROMPT_CATEGORIES

    @pytest.mark.asyncio
    async def test_unknown_category_skipped(self, db: Database):
        await log_prompt(db, category="bogus_category", prompt_text="nope")

        rows = await db.fetch_all("SELECT * FROM prompt_history")
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_db_error_swallowed(self, db: Database):
        """Errors during INSERT must not propagate."""
        # Use a closed DB to force a failure path
        await db.close()
        # Should not raise
        await log_prompt(db, category="plan_generation", prompt_text="oops")

    @pytest.mark.asyncio
    async def test_optional_fields_default_to_none(self, db: Database):
        await log_prompt(db, category="notification", prompt_text="hello")

        rows = await db.fetch_all("SELECT * FROM prompt_history")
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["project_id"] is None
        assert row["task_id"] is None
        assert row["session_key"] is None


# ---------------------------------------------------------------------------
# Integration tests — reasoning service
# ---------------------------------------------------------------------------

class TestReasoningServiceLogging:
    @pytest.mark.asyncio
    async def test_evaluate_logs_prompt(self, db: Database):
        from cyborg.services.project_service import ProjectService
        from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService
        from cyborg.services.openclaw_hook_service import OpenClawHookService
        from cyborg.models import ProjectCreate, SuccessCriterion

        project_service = ProjectService(db)
        project = await project_service.create_project(ProjectCreate(
            title="Log Test Project",
            aim="Verify prompt logging",
            success_criteria=[
                SuccessCriterion(check="done == true", description="It is done"),
            ],
        ))
        project_id = str(project.id)

        reasoning_service = OpenClawReasoningService(db)

        mock_response = {
            "content": '{"all_met": true, "met_criteria": [], "unmet_criteria": [], "reasoning": "OK"}'
        }

        with patch.object(
            OpenClawHookService,
            "_send_gateway_request",
            new=AsyncMock(return_value=mock_response),
        ):
            await reasoning_service.evaluate_success_criteria(project_id)

        rows = await db.fetch_all(
            "SELECT * FROM prompt_history WHERE category = 'criteria_evaluation'"
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["project_id"] == project_id
        assert "valid JSON only" in row["prompt_text"]

    @pytest.mark.asyncio
    async def test_generate_project_plan_logs_prompt(self, db: Database):
        from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService
        from cyborg.services.openclaw_hook_service import OpenClawHookService

        reasoning_service = OpenClawReasoningService(db)

        mock_response = {
            "content": '{"steps": [{"order": 0, "title": "Step 1", "description": "Do it", "criteria": "Done"}]}'
        }

        with patch.object(
            OpenClawHookService,
            "_send_gateway_request",
            new=AsyncMock(return_value=mock_response),
        ):
            await reasoning_service.generate_project_plan(
                aim="Build a portal",
                method="FastAPI",
                success_criteria=["Handles users"],
            )

        rows = await db.fetch_all(
            "SELECT * FROM prompt_history WHERE category = 'plan_generation'"
        )
        assert len(rows) == 1
        assert "Build a portal" in dict(rows[0])["prompt_text"]


# ---------------------------------------------------------------------------
# Integration tests — hook service
# ---------------------------------------------------------------------------

class TestHookServiceLogging:
    @pytest.mark.asyncio
    async def test_task_assignment_logs_prompt(self, db: Database):
        from cyborg.services.openclaw_hook_service import OpenClawHookService
        from cyborg.services.session_route_service import SessionRouteService
        from cyborg.models import NotificationType

        routing = SessionRouteService(db)
        hook = OpenClawHookService(db, routing_service=routing)

        notification = {
            "id": "notif-1",
            "title": "Do something",
            "message": "Details here",
            "notification_type": NotificationType.TASK_ASSIGNMENT.value,
            "entity_id": "entity-1",
            "metadata": {
                "delivery_route": "target",
                "task_id": "task-1",
                "project_id": "proj-1",
                "channel": "whatsapp",
                "session_key": "sess-src",
                "chat_id": "chat-1",
                "target_session": {"kind": "whatsapp"},
            },
        }

        # Resolve route returns a model; we mock it to provide a usable route
        mock_route = MagicMock()
        mock_route.model_dump.return_value = {
            "channel": "whatsapp",
            "to": "+1234567890",
            "session_key": "sess-dst",
        }

        with patch.object(
            routing, "resolve_notification_route", return_value=mock_route
        ), patch.object(
            routing, "resolve_target_session_key", return_value="sess-dst"
        ), patch.object(
            hook, "_send_gateway_request", new=AsyncMock(return_value={})
        ):
            await hook.dispatch_notification(notification)

        rows = await db.fetch_all(
            "SELECT * FROM prompt_history WHERE category = 'task_assignment'"
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["session_key"] == "sess-dst"
        assert row["task_id"] == "task-1"
        assert "task assignment" in row["prompt_text"].lower()

    @pytest.mark.asyncio
    async def test_needs_input_logs_prompt(self, db: Database):
        from cyborg.services.openclaw_hook_service import OpenClawHookService
        from cyborg.services.session_route_service import SessionRouteService

        routing = SessionRouteService(db)
        hook = OpenClawHookService(db, routing_service=routing)

        notification = {
            "id": "notif-2",
            "title": "Plan approval needed",
            "message": "Please approve the plan",
            "notification_type": "needs_input",
            "entity_type": "task",
            "metadata": {
                "task_id": "task-2",
                "project_id": "proj-2",
                "channel": "whatsapp",
                "session_key": "sess-src",
            },
        }

        mock_route = MagicMock()
        mock_route.model_dump.return_value = {
            "channel": "whatsapp",
            "to": "+1234567890",
            "session_key": "sess-ni",
        }

        with patch.object(
            routing, "resolve_notification_route", return_value=mock_route
        ), patch.object(
            hook, "_send_gateway_request", new=AsyncMock(return_value={})
        ):
            await hook.dispatch_notification(notification)

        rows = await db.fetch_all(
            "SELECT * FROM prompt_history WHERE category = 'needs_input'"
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["session_key"] == "sess-ni"
        assert row["task_id"] == "task-2"

    @pytest.mark.asyncio
    async def test_generic_notification_logs_prompt(self, db: Database):
        from cyborg.services.openclaw_hook_service import OpenClawHookService
        from cyborg.services.session_route_service import SessionRouteService

        routing = SessionRouteService(db)
        hook = OpenClawHookService(db, routing_service=routing)

        notification = {
            "id": "notif-3",
            "title": "Status Update",
            "message": "Task completed successfully",
            "notification_type": "info",
            "entity_type": "task",
            "metadata": {
                "project_id": "proj-3",
                "task_id": "task-3",
                "channel": "whatsapp",
                "session_key": "sess-gen",
            },
        }

        mock_route = MagicMock()
        mock_route.model_dump.return_value = {
            "channel": "whatsapp",
            "to": "+1234567890",
            "session_key": "sess-gen-vis",
        }

        with patch.object(
            routing, "resolve_notification_route", return_value=mock_route
        ), patch.object(
            hook, "_send_gateway_request", new=AsyncMock(return_value={})
        ):
            await hook.dispatch_notification(notification)

        rows = await db.fetch_all(
            "SELECT * FROM prompt_history WHERE category = 'notification'"
        )
        assert len(rows) == 1
        row = dict(rows[0])
        assert "Status Update" in row["prompt_text"]
        assert row["session_key"] == "sess-gen-vis"
