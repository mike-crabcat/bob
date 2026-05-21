"""Skill delegation eval cases with mocked handlers."""

import json

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case


def _make_mock_delegation_tools():
    from cyborg_server.services.tools import tool

    _delegations: dict[str, dict] = {}

    @tool
    async def delegate_to_claude(user_story: str) -> str:
        """Submit a user story to Claude Code for skill planning.
        Describe the capability you need as a clear user story.
        Returns a plan for your review — call implement_delegation to proceed."""
        import uuid
        delegation_id = str(uuid.uuid4())[:8]
        _delegations[delegation_id] = {
            "id": delegation_id,
            "user_story": user_story,
            "plan": f"Plan: Create a skill for the requested capability.",
            "status": "plan_ready",
        }
        return json.dumps({
            "ok": True,
            "delegation_id": delegation_id,
            "plan": "Will create a new skill with skill.md defining the capability and any needed helper scripts.",
        })

    @tool
    async def implement_delegation(delegation_id: str) -> str:
        """Approve and execute a delegation plan. Claude Code will create the skill files."""
        if delegation_id not in _delegations:
            return json.dumps({"ok": False, "error": "Delegation not found"})
        _delegations[delegation_id]["status"] = "completed"
        return json.dumps({
            "ok": True,
            "delegation_id": delegation_id,
            "result": "Skill created successfully.",
            "files_created": ["example_skill"],
        })

    @tool
    async def reject_delegation(delegation_id: str, reason: str) -> str:
        """Reject a delegation plan with feedback."""
        return json.dumps({"ok": True, "delegation_id": delegation_id, "status": "rejected"})

    @tool
    async def list_delegations(status: str = "") -> str:
        """List skill delegations, optionally filtered by status."""
        return json.dumps(list(_delegations.values()))

    return [delegate_to_claude, implement_delegation, reject_delegation, list_delegations]


def _make_mock_workspace_tools():
    from cyborg_server.services.tools import tool

    @tool
    async def list_files(path: str = "", depth: int = 1) -> str:
        """List files and directories in the workspace."""
        return json.dumps([{"name": "skills/", "type": "dir"}])

    @tool
    async def read_file(path: str) -> str:
        """Read the contents of a file in the workspace."""
        return "No content"

    @tool
    async def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace."""
        return json.dumps({"ok": True, "path": path, "bytes": len(content)})

    @tool
    async def send_whatsapp_message(text: str) -> str:
        """Send a reply message to the WhatsApp chat."""
        return f"Message sent"

    return [list_files, read_file, write_file, send_whatsapp_message]


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


AGENDA = """\
You are managing a WhatsApp conversation. An incoming message has been received.

Your role: read the message and respond appropriately.

AVAILABLE CAPABILITIES:
- Use the send_whatsapp_message tool to reply in this conversation.
- If you need a capability you don't have, use delegate_to_claude with a clear
  user story describing the skill. Review the plan, then implement_delegation to proceed.
"""


@eval_case(
    id="delegation_new_skill",
    category="skill_delegation",
    description="LLM should delegate to Claude when asked for a capability it doesn't have.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "delegate_to_claude"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called delegate_to_claude with a user story describing "
            "the requested capability (stock price lookup). It should NOT attempt to answer "
            "the question directly since it lacks that capability."
        ),
    ),
)
async def delegation_new_skill(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": AGENDA},
        {"role": "user", "content": "Can you look up the current AAPL stock price?"},
    ]

    tools = _make_mock_workspace_tools() + _make_mock_delegation_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}


@eval_case(
    id="delegation_implement_after_plan",
    category="skill_delegation",
    description="LLM should call implement_delegation after user approves the plan.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "delegate_to_claude"}),
        StructuralCheck(kind="tool_call_made", params={"tool_name": "implement_delegation"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should first call delegate_to_claude, then when the user approves, "
            "call implement_delegation with the delegation_id. This is a two-step flow."
        ),
    ),
)
async def delegation_implement_after_plan(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": AGENDA},
        {"role": "user", "content": "I need you to be able to check cryptocurrency prices."},
    ]

    tools = _make_mock_workspace_tools() + _make_mock_delegation_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    # Simulate the user approving and ask to implement
    messages.append({"role": "user", "content": "That plan looks good, go ahead and implement it."})
    response2 = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response2, "context": {"tool_calls": tool_calls}, "input_messages": messages}
