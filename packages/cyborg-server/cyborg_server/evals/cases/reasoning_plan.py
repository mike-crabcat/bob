"""Reasoning plan generation eval cases."""

import json

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case


@eval_case(
    id="reasoning_plan_basic",
    category="reasoning_plan",
    description="Generate a project plan with structured steps.",
    structural_checks=[
        StructuralCheck(kind="json_valid"),
        StructuralCheck(kind="json_schema", params={
            "required_fields": ["steps"],
            "array_field": "steps",
            "min_items": 3,
            "max_items": 8,
            "item_required_fields": ["title", "description"],
        }),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Steps should be logically ordered and actionable. "
            "Each step should be specific enough to execute. "
            "The plan should address the stated aim and success criteria."
        ),
    ),
)
async def reasoning_plan_basic(ctx):
    from cyborg_server.services.openclaw_reasoning_service import OpenClawReasoningService

    svc = OpenClawReasoningService(ctx)
    steps = await svc.generate_project_plan(
        aim="Set up a CI/CD pipeline for a Python web application.",
        method="Configure GitHub Actions for testing and deployment.",
        success_criteria=[
            "Automated tests run on every pull request",
            "Successful deploys to staging on merge to main",
        ],
    )
    return {"response": json.dumps({"steps": steps})}


@eval_case(
    id="reasoning_plan_with_sources",
    category="reasoning_plan",
    description="Plan generation should leverage source project outputs.",
    structural_checks=[
        StructuralCheck(kind="json_valid"),
        StructuralCheck(kind="json_schema", params={
            "required_fields": ["steps"],
            "array_field": "steps",
            "min_items": 2,
        }),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The plan should reference reusing existing outputs (scripts, venv, reports) "
            "from the source project. Score higher if the plan avoids duplicating work."
        ),
    ),
)
async def reasoning_plan_with_sources(ctx):
    from cyborg_server.services.openclaw_reasoning_service import OpenClawReasoningService

    svc = OpenClawReasoningService(ctx)
    steps = await svc.generate_project_plan(
        aim="Add monitoring dashboard to the web application.",
        source_context={
            "source_projects": [{
                "title": "CI/CD Pipeline Setup",
                "aim": "Set up CI/CD",
                "conclusion": "Pipeline configured with GitHub Actions",
                "relevance_reason": "Has deployment scripts that can be extended",
                "outputs": [
                    {"type": "script", "path": "/scripts/deploy.sh", "description": "Deployment script"},
                ],
            }],
        },
    )
    return {"response": json.dumps({"steps": steps})}
