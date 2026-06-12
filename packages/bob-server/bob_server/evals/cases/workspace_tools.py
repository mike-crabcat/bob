"""Workspace file tool eval cases with mocked handlers."""

import json

from bob_server.evals.case import JudgeCriteria, StructuralCheck
from bob_server.evals.registry import eval_case

_MOCK_FILE_TREE = json.dumps([
    {"name": "SOUL.md", "type": "file", "size_bytes": 3120},
    {"name": "IDENTITY.md", "type": "file", "size_bytes": 1850},
    {"name": "AGENTS.md", "type": "file", "size_bytes": 940},
    {"name": "USER.md", "type": "file", "size_bytes": 620},
    {"name": "notes", "type": "dir"},
])

_MOCK_FILE_CONTENT = (
    "# SOUL.md - Bob Jr (Bob)\n\n"
    "## Core Persona\n"
    "You are a senior-level, high-efficiency thinking partner.\n"
)


def _make_mock_workspace_tools():
    from bob_server.services.tools import tool

    @tool
    async def ls(path: str = "") -> str:
        """List files and directories in a single workspace directory (non-recursive)."""
        return _MOCK_FILE_TREE

    @tool
    async def read_file(path: str) -> str:
        """Read the contents of a file in the workspace."""
        return _MOCK_FILE_CONTENT

    @tool
    async def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace. Creates parent directories if needed."""
        return json.dumps({"ok": True, "path": path, "bytes": len(content)})

    return [ls, read_file, write_file]


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
    id="workspace_ls",
    category="workspace_tools",
    description="LLM should call ls when asked what files are in the workspace.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "ls"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called ls (with no args or path=''). "
            "The final response should mention the files found."
        ),
    ),
)
async def workspace_ls(ctx):
    from bob_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": "You are an AI assistant with access to workspace file tools. Use them when asked about files."},
        {"role": "user", "content": "What files are in my workspace?"},
    ]

    tools = _make_mock_workspace_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}


@eval_case(
    id="workspace_read_file",
    category="workspace_tools",
    description="LLM should call read_file when asked to show a specific file's contents.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "read_file"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called read_file with path='SOUL.md'. "
            "The response should include or reference the file contents."
        ),
    ),
)
async def workspace_read_file(ctx):
    from bob_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": "You are an AI assistant with access to workspace file tools. Use them when asked about files."},
        {"role": "user", "content": "Can you show me what's in SOUL.md?"},
    ]

    tools = _make_mock_workspace_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}


@eval_case(
    id="workspace_write_file",
    category="workspace_tools",
    description="LLM should call write_file when asked to create or update a file.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "write_file"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called write_file with a sensible path and content. "
            "The response should confirm the file was written."
        ),
    ),
)
async def workspace_write_file(ctx):
    from bob_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": "You are an AI assistant with access to workspace file tools. Use them when asked to modify files."},
        {"role": "user", "content": "Create a file called notes/todo.md with a quick to-do list for today: review PRs, deploy staging, update docs."},
    ]

    tools = _make_mock_workspace_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}
