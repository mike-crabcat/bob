"""Reasoning next-step decision eval cases."""

import json

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case


@eval_case(
    id="reasoning_next_step_basic",
    category="reasoning_next_step",
    description="Decide the next action after a task completes.",
    structural_checks=[
        StructuralCheck(kind="json_valid"),
        StructuralCheck(kind="field_present", params={"fields": ["action", "reasoning"]}),
        StructuralCheck(kind="field_values", params={
            "field": "action",
            "allowed": ["create_task", "close_project", "block_project"],
        }),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The action should be appropriate for the context. "
            "If the project has more work to do, create_task is correct. "
            "If all criteria are met, close_project is correct. "
            "Score the quality of the reasoning regardless of the action chosen."
        ),
    ),
)
async def reasoning_next_step_basic(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    prompt = (
        "You are managing an autonomous project. A task has just completed.\n\n"
        "Project: Website Redesign\n"
        "Aim: Redesign the company website with improved UX\n\n"
        "Plan:\n"
        "  1. Research competitors\n"
        "  2. Create wireframes\n"
        "  3. Build the new design\n\n"
        "Success Criteria:\n"
        "  1. Competitive analysis report delivered\n"
        "  2. Wireframes approved by stakeholders\n"
        "  3. New design live and accessible\n\n"
        "Just Completed: Research competitors — Analyzed 5 competitors and documented findings.\n\n"
        "Based on the above, decide the single best next action.\n\n"
        "Respond with valid JSON only:\n"
        '{"action": "create_task|close_project|block_project", "reasoning": "...", '
        '"task": {"title": "...", "description": "...", "plan": "...", "priority": "..."}}'
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(
        [{"role": "user", "content": prompt}],
        call_category="eval",
    )
    return {"response": response}
