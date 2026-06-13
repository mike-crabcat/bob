"""Workspace bash tool eval cases with mocked handlers."""

from bob_server.evals.case import JudgeCriteria, StructuralCheck
from bob_server.evals.registry import eval_case

_MOCK_LS_OUTPUT = (
    "SOUL.md\n"
    "IDENTITY.md\n"
    "AGENTS.md\n"
    "USER.md\n"
    "notes/\n"
)


def _make_mock_workspace_tools():
    from bob_server.services.tools import tool

    @tool
    async def bash(command: str) -> str:
        """Run a bash command in the workspace directory."""
        return _MOCK_LS_OUTPUT

    return [bash]


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


@eval_case(
    id="workspace_bash",
    category="workspace_tools",
    description="LLM should call bash when asked to list or inspect workspace files.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "bash"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called bash with a command like 'ls' (or similar) "
            "to inspect the workspace. The final response should mention the files found."
        ),
    ),
)
async def workspace_bash(ctx):
    from bob_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": "You are an AI assistant with access to a bash tool that runs commands in the workspace. Use it when asked about files."},
        {"role": "user", "content": "What files are in my workspace?"},
    ]

    tools = _make_mock_workspace_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}
