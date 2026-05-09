"""Tool calling eval cases."""

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case


@eval_case(
    id="tool_calling_create_task",
    category="tool_calling",
    description="LLM should use create_task tool when asked to add a task.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "create_task"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called create_task with a sensible title. "
            "The final text response should confirm the task was created."
        ),
    ),
)
async def tool_calling_create_task(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService
    from cyborg_server.services.project_tools import make_project_tools
    from cyborg_server.services.project_service import ProjectService

    project_svc = ProjectService(ctx)
    project = await project_svc.create_project({
        "title": "Eval Test Project",
        "aim": "Test project for tool calling eval.",
        "description": "Temporary project for eval.",
    })
    project_id = str(project.id)

    messages = [
        {"role": "system", "content": (
            "You are a project management assistant. "
            "Use the available tools to manage tasks."
        )},
        {"role": "user", "content": (
            f"Please create a new task called 'Write unit tests' in project {project_id}. "
            "Set priority to high and add a brief description about testing the API endpoints."
        )},
    ]

    tools = make_project_tools(ctx)
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(
        messages, tools,
        call_category="eval",
        project_id=project_id,
    )

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}


@eval_case(
    id="tool_calling_close_project",
    category="tool_calling",
    description="LLM should use close_project tool when told a project is done.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "close_project"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called close_project with the correct project_id. "
            "A conclusion should be provided."
        ),
    ),
)
async def tool_calling_close_project(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService
    from cyborg_server.services.project_tools import make_project_tools
    from cyborg_server.services.project_service import ProjectService

    project_svc = ProjectService(ctx)
    project = await project_svc.create_project({
        "title": "Eval Close Project",
        "aim": "Test project for closing eval.",
    })
    project_id = str(project.id)

    messages = [
        {"role": "system", "content": "You are a project management assistant. Use the available tools."},
        {"role": "user", "content": (
            f"All tasks are complete for project {project_id}. "
            "Please close it with a brief conclusion."
        )},
    ]

    tools = make_project_tools(ctx)
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(
        messages, tools,
        call_category="eval",
        project_id=project_id,
    )

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}


def _extract_tool_calls(messages: list) -> list[dict]:
    calls = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                calls.append({
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                })
    return calls
