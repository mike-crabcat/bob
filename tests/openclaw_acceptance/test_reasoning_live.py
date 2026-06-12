from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from bob_server.models import JournalEntryType


pytestmark = pytest.mark.openclaw_live


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _skip_if_live_backend_unavailable(exc: BaseException) -> None:
    message = str(exc).lower()
    if any(fragment in message for fragment in ("429", "rate limit", "usage limit reached", "quota")):
        pytest.skip(f"OpenClaw model backend is currently unavailable: {exc}")
    if "gateway" in message and "timed out" in message:
        pytest.skip(f"OpenClaw gateway timed out during acceptance test: {exc}")


def _configure_reasoning_session(monkeypatch: pytest.MonkeyPatch, live_reasoning_service: Any, live_openclaw: Any, purpose: str) -> str:
    session_key = live_openclaw.new_session_key(f"reasoning-{purpose}")
    hook_service = live_reasoning_service.openclaw_service
    original_gateway = hook_service._send_gateway_request

    async def logged_gateway(
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        live_openclaw.write_artifact(
            f"{purpose}_gateway_request.json",
            {
                "method": method,
                "params": params,
                "expect_final": expect_final,
                "timeout_seconds": timeout_seconds,
            },
        )
        response = await original_gateway(
            method,
            params,
            expect_final=expect_final,
            timeout_seconds=timeout_seconds,
        )
        live_openclaw.write_artifact(f"{purpose}_gateway_response.json", response)
        return response

    monkeypatch.setattr(hook_service, "_send_gateway_request", logged_gateway)
    original_call = live_reasoning_service._call_openclaw

    async def isolated_call(
        prompt: str,
        response_format: str = "text",
        timeout: int = 30,
        session_key_override: str | None = None,
    ) -> str:
        live_openclaw.write_artifact(f"{purpose}_prompt.txt", prompt)
        response = await original_call(
            prompt,
            response_format=response_format,
            timeout=timeout,
            session_key=session_key_override or session_key,
        )
        live_openclaw.write_artifact(f"{purpose}_parsed_response.txt", response)
        return response

    monkeypatch.setattr(live_reasoning_service, "_call_openclaw", isolated_call)
    return session_key


def _line_count(text: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines)


def _has_semantic_overlap(text: str, expected_terms: set[str]) -> bool:
    tokens = {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}
    return len(tokens & expected_terms) >= 2


def test_live_generate_project_plan(monkeypatch: pytest.MonkeyPatch, live_reasoning_service: Any, live_openclaw: Any) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "generate_project_plan")
    try:
        steps = _run(
            live_reasoning_service.generate_project_plan(
                aim="Launch a small customer feedback sprint for a cafe and produce a prioritized improvement list.",
                method="Interview customers, cluster themes, and summarize the top actions.",
                success_criteria=[
                    "At least 5 pieces of customer feedback collected",
                    "Three prioritized improvement actions documented",
                ],
            )
        )
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert 3 <= len(steps) <= 8
    for index, step in enumerate(steps):
        assert step["order"] == index
        assert str(step["title"]).strip()
        assert str(step["description"]).strip()
        assert str(step["criteria"]).strip()


def test_live_evaluate_success_criteria_met(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "evaluate_success_criteria_met")
    project = acceptance_builder.create_project(
        title="Acceptance Criteria Met",
        description="Project used to verify live criteria evaluation.",
        aim="Collect and summarize three useful customer observations.",
        method="Interview two people, then summarize the observations in a note.",
        success_criteria=[
            {"check": "completed_tasks >= 2", "description": "Complete at least 2 tasks"},
            {"check": "failed_tasks == 0", "description": "No failed tasks"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    acceptance_builder.add_project_journal_entry(
        project_id,
        entry_type=JournalEntryType.NOTE,
        content="Collected feedback from two customers and summarized the main pain points.",
    )
    acceptance_builder.create_task(
        title="Interview customer one",
        description="Ask about ordering and pickup experience.",
        plan="Call the customer, gather two observations, summarize the notes.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="Customer one reported slow pickup and liked the menu variety.",
    )
    acceptance_builder.create_task(
        title="Interview customer two",
        description="Ask about product quality and service.",
        plan="Call the customer, gather two observations, summarize the notes.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="Customer two praised service quality and suggested clearer pickup signage.",
    )

    try:
        evaluation = _run(live_reasoning_service.evaluate_success_criteria(project_id))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert evaluation["all_met"] is True
    met_criteria = " ".join(evaluation.get("met_criteria", [])).lower()
    assert "complete at least 2 tasks" in met_criteria
    assert "no failed tasks" in met_criteria
    assert str(evaluation.get("reasoning", "")).strip()


def test_live_evaluate_success_criteria_unmet(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "evaluate_success_criteria_unmet")
    project = acceptance_builder.create_project(
        title="Acceptance Criteria Unmet",
        aim="Prepare a tiny launch checklist.",
        method="Complete two verification tasks with no failures.",
        success_criteria=[
            {"check": "completed_tasks >= 2", "description": "Complete at least 2 tasks"},
            {"check": "failed_tasks == 0", "description": "No failed tasks"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    acceptance_builder.create_task(
        title="Draft checklist",
        description="Create the initial launch checklist.",
        plan="Draft the checklist and save it.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="Drafted the initial checklist.",
    )
    acceptance_builder.create_task(
        title="Verify launch links",
        description="Check the launch links.",
        plan="Verify all launch links and record any broken links.",
        project_ids=[project_id],
        approve_plan=True,
        fail_result="Two launch links were broken.",
    )

    try:
        evaluation = _run(live_reasoning_service.evaluate_success_criteria(project_id))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert evaluation["all_met"] is False
    unmet_criteria = " ".join(evaluation.get("unmet_criteria", [])).lower()
    assert "no failed tasks" in unmet_criteria or "complete at least 2 tasks" in unmet_criteria
    assert str(evaluation.get("reasoning", "")).strip()


def test_live_generate_follow_up_tasks(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "generate_follow_up_tasks")
    project = acceptance_builder.create_project(
        title="Follow-up Tasks Project",
        aim="Get sign-off on a release checklist and gather legal confirmation.",
        method="Prepare the checklist, then collect the missing approvals.",
        success_criteria=[
            {"check": "completed_tasks >= 2", "description": "Checklist and approval work completed"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    acceptance_builder.add_project_journal_entry(
        project_id,
        entry_type=JournalEntryType.BLOCKER,
        content="The legal team has not confirmed the disclaimer wording yet.",
    )
    acceptance_builder.create_task(
        title="Draft the release checklist",
        description="Prepare the release checklist draft.",
        plan="Draft the checklist and circulate it internally.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="Drafted the checklist and circulated it internally.",
    )

    unmet_criteria = [
        "Receive legal approval for the disclaimer wording",
        "Confirm the final release checklist with operations",
    ]
    try:
        tasks = _run(live_reasoning_service.generate_follow_up_tasks(project_id, unmet_criteria))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert tasks
    expected_terms = {"legal", "approval", "disclaimer", "release", "checklist", "operations", "confirm"}
    assert any(_has_semantic_overlap(" ".join(task.values()).lower(), expected_terms) for task in tasks)
    for task in tasks:
        assert str(task.get("title", "")).strip()
        assert str(task.get("description", "")).strip()
        assert str(task.get("plan", "")).strip()
        assert task.get("priority") in {"low", "medium", "high", "critical"}


def test_live_refine_project_strategy_healthy(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "refine_project_strategy_healthy")
    project = acceptance_builder.create_project(
        title="Healthy Strategy Project",
        aim="Prepare a simple onboarding guide.",
        method="Collect source notes, draft the guide, then review it.",
        success_criteria=[
            {"check": "completed_tasks >= 2", "description": "Core onboarding work completed"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    task = acceptance_builder.create_task(
        title="Draft onboarding guide",
        description="Write the first draft of the guide.",
        plan="Collect notes and draft the guide.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="Drafted the onboarding guide and shared it for review.",
    )

    try:
        result = _run(live_reasoning_service.refine_project_strategy(project_id, task["id"]))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert str(result.get("reasoning", "")).strip()
    if result.get("should_refine") is False:
        assert not result.get("suggested_changes")


def test_live_refine_project_strategy_degraded(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "refine_project_strategy_degraded")
    project = acceptance_builder.create_project(
        title="Degraded Strategy Project",
        aim="Prepare an event plan.",
        method="Confirm venue details, then coordinate suppliers.",
        success_criteria=[
            {"check": "completed_tasks >= 2", "description": "Venue and supplier coordination completed"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    task = acceptance_builder.create_task(
        title="Confirm the venue details",
        description="The venue details are still uncertain.",
        plan="Confirm date, room availability, and AV support.",
        project_ids=[project_id],
        approve_plan=True,
        fail_result="The venue cancelled the original booking and no replacement has been confirmed.",
    )
    acceptance_builder.add_project_journal_entry(
        project_id,
        entry_type=JournalEntryType.BLOCKER,
        content="Venue fell through, and supplier scheduling is now at risk.",
    )

    try:
        result = _run(live_reasoning_service.refine_project_strategy(project_id, task["id"]))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert str(result.get("reasoning", "")).strip()
    assert (
        result.get("should_refine") is True
        or bool(result.get("suggested_changes"))
        or bool(result.get("risks_identified"))
    )


def test_live_extract_learnings(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "extract_learnings")
    project = acceptance_builder.create_project(
        title="Learning Extraction Project",
        aim="Run a small experiment and capture lessons.",
        method="Complete one experiment, note the blocker, then close the project.",
        success_criteria=[
            {"check": "completed_tasks >= 1", "description": "Complete the experiment task"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    acceptance_builder.create_task(
        title="Run the experiment",
        description="Execute the small experiment.",
        plan="Run the experiment and summarize what happened.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="The experiment worked, but setup time was longer than expected.",
    )
    acceptance_builder.create_task(
        title="Investigate setup delay",
        description="Look into why setup took longer than expected.",
        plan="Review the setup steps and identify the delay.",
        project_ids=[project_id],
        approve_plan=True,
        fail_result="Could not reproduce the delay because the original environment was gone.",
    )
    acceptance_builder.add_project_journal_entry(
        project_id,
        entry_type=JournalEntryType.RESULT,
        content="The experiment worked well after setup, but setup documentation was incomplete.",
    )
    acceptance_builder.add_project_journal_entry(
        project_id,
        entry_type=JournalEntryType.DECISION,
        content="Future experiments should include a setup checklist before execution begins.",
    )
    acceptance_builder.close_project(project_id, conclusion="Experiment closed with useful lessons about setup discipline.")

    try:
        insights = _run(live_reasoning_service.extract_learnings(project_id))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert insights
    for insight in insights:
        assert str(insight.get("category", "")).strip()
        assert str(insight.get("lesson", "")).strip()
        assert str(insight.get("applicability", "")).strip()
        assert str(insight.get("impact", "")).strip()


def test_live_generate_task_plan(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "generate_task_plan")
    project = acceptance_builder.create_project(
        title="Task Planning Parent Project",
        aim="Prepare a venue shortlist for a community meetup.",
        method="Gather options, compare them, and propose the best fit.",
        success_criteria=[
            {"check": "completed_tasks >= 1", "description": "A venue shortlist is prepared"},
        ],
        approve_spec=True,
    )
    task = acceptance_builder.create_task(
        title="Prepare a venue shortlist",
        description="Find three realistic meetup venues and compare them on cost, capacity, and transport access.",
        plan="Initial placeholder plan that should be improved.",
        project_ids=[project["id"]],
    )

    try:
        plan = _run(live_reasoning_service.generate_task_plan(task["id"]))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert plan.strip()
    assert _line_count(plan) >= 3
    lowered = plan.lower()
    assert "venue" in lowered or "meetup" in lowered or "shortlist" in lowered


def test_live_analyze_project_health(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_builder: Any,
    live_reasoning_service: Any,
    live_openclaw: Any,
) -> None:
    _configure_reasoning_session(monkeypatch, live_reasoning_service, live_openclaw, "analyze_project_health")
    project = acceptance_builder.create_project(
        title="Health Analysis Project",
        aim="Prepare a supplier handoff package.",
        method="Collect documents, validate them, and send the handoff package.",
        success_criteria=[
            {"check": "completed_tasks >= 2", "description": "Core handoff tasks completed"},
        ],
        approve_spec=True,
    )
    project_id = project["id"]
    acceptance_builder.create_task(
        title="Collect supplier documents",
        description="Gather the required supplier documents.",
        plan="Collect the documents and record any missing items.",
        project_ids=[project_id],
        approve_plan=True,
        complete_result="Collected most supplier documents, but the insurance certificate is still missing.",
    )
    blocked = acceptance_builder.create_task(
        title="Validate supplier documents",
        description="Validate the documents before handoff.",
        plan="Review the documents and confirm the supplier is ready.",
        project_ids=[project_id],
        approve_plan=True,
    )
    acceptance_builder.block_task(
        blocked["id"],
        reason="The supplier insurance certificate is missing.",
        resume_instructions="Resume once the supplier uploads the current insurance certificate.",
    )
    acceptance_builder.add_project_journal_entry(
        project_id,
        entry_type=JournalEntryType.BLOCKER,
        content="Supplier handoff is blocked until the insurance certificate arrives.",
    )

    try:
        health = _run(live_reasoning_service.analyze_project_health(project_id))
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    assert str(health.get("reasoning", "")).strip()
    assert (
        health.get("risk_level") in {"medium", "high", "critical"}
        or bool(health.get("blockers"))
        or bool(health.get("recommendations"))
        or health.get("escalation_required") is True
    )
