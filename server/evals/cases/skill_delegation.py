"""Subagent eval cases with mocked handlers."""

import json

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case


def _make_mock_subagent_tools():
    from server.services.tools import tool

    _subagents: dict[str, dict] = {}

    @tool
    async def create_subagent(task: str) -> str:
        """Spawn a subagent to work on a task asynchronously. Returns subagent_id immediately.
        After calling this, you MUST send a message to the user summarizing what you delegated.
        Use check_subagent to poll for results and message_subagent for follow-up."""
        import uuid
        subagent_id = str(uuid.uuid4())[:8]
        _subagents[subagent_id] = {
            "id": subagent_id,
            "task": task,
            "status": "waiting_for_parent",
            "result": f"Completed task: {task[:50]}",
        }
        return json.dumps({
            "ok": True,
            "subagent_id": subagent_id,
            "status": "created",
        })

    @tool
    async def check_subagent(subagent_id: str) -> str:
        """Check the status and result of a subagent."""
        if subagent_id not in _subagents:
            return json.dumps({"ok": False, "error": "Subagent not found"})
        return json.dumps({"ok": True, **_subagents[subagent_id]})

    @tool
    async def message_subagent(subagent_id: str, message: str) -> str:
        """Send a follow-up message to a subagent."""
        if subagent_id not in _subagents:
            return json.dumps({"ok": False, "error": "Subagent not found"})
        _subagents[subagent_id]["result"] += f"\nFollow-up: {message[:50]}"
        return json.dumps({
            "ok": True,
            "result": _subagents[subagent_id]["result"],
            "subagent_id": subagent_id,
        })

    @tool
    async def list_subagents(status: str = "") -> str:
        """List your subagents, optionally filtered by status."""
        return json.dumps(list(_subagents.values()))

    @tool
    async def kill_subagent(subagent_id: str) -> str:
        """Kill a running subagent."""
        if subagent_id in _subagents:
            _subagents[subagent_id]["status"] = "killed"
        return json.dumps({"ok": True, "subagent_id": subagent_id, "status": "killed"})

    return [create_subagent, check_subagent, message_subagent, list_subagents, kill_subagent]


def _make_mock_workspace_tools():
    from server.services.tools import tool

    @tool
    async def ls(path: str = "") -> str:
        """List files and directories in a single workspace directory (non-recursive)."""
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
        return "Message sent"

    return [ls, read_file, write_file, send_whatsapp_message]


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

DELIVERY: Your FINAL text output is your reply — it is delivered automatically.
Call send_whatsapp_message only to speak BEFORE you finish: a brief progress update while working,
or a reply with media attached. To stay silent, finish with the exact text NO_REPLY.

AVAILABLE CAPABILITIES:
- You have subagent capabilities. Use create_subagent(task) to spawn a background worker.
  IMPORTANT: After creating a subagent, tell the user what you've delegated and that work
  is underway — either a brief progress update via send_whatsapp_message, or by finishing
  with a short reply. Do NOT go silent while work runs.
  When a subagent replies, you will receive its message automatically. You can then:
  - message_subagent(id, message) to continue the conversation and give further instructions
  - kill_subagent(id) if the task is no longer needed
  - Or simply use the result and move on — no action needed if the task is complete.
  Subagents are expensive — only use them for tasks you cannot do yourself.
"""


@eval_case(
    id="subagent_create_task",
    category="skill_delegation",
    description="LLM should create a subagent when asked for a capability it doesn't have.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "create_subagent"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called create_subagent with a task describing "
            "the requested capability (stock price lookup). The delegation ack "
            "counts either way under final-text delivery: a brief progress "
            "update via send_whatsapp_message, or a short final-text reply "
            "saying the work is underway (the response text itself may be "
            "the ack). It should NOT attempt to answer the question directly "
            "since it lacks that capability, and should NOT go silent."
        ),
    ),
)
async def subagent_create_task(ctx):
    from server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": AGENDA},
        {"role": "user", "content": "Can you look up the current AAPL stock price?"},
    ]

    tools = _make_mock_workspace_tools() + _make_mock_subagent_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.run_turn(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response, "context": {"tool_calls": tool_calls}, "input_messages": messages}


@eval_case(
    id="subagent_follow_up",
    category="skill_delegation",
    description="LLM should message_subagent after receiving a subagent result and user asks for more.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "message_subagent"}),
        StructuralCheck(kind="min_length", params={"min_length": 10}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should call message_subagent with the subagent_id and a follow-up message "
            "when the user asks for additional work on the same task."
        ),
    ),
)
async def subagent_follow_up(ctx):
    from server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": AGENDA},
        {"role": "user", "content": "I need you to be able to check cryptocurrency prices."},
    ]

    tools = _make_mock_workspace_tools() + _make_mock_subagent_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.run_turn(messages, tools, call_category="eval")

    # Simulate a subagent result arriving
    messages.append({
        "role": "user",
        "content": "[Subagent abc12345] I've created a skill that can check crypto prices.\n\n(use message_subagent to reply, or kill_subagent to terminate)",
    })
    messages.append({
        "role": "user",
        "content": "Great! Can you also add support for checking the market cap?",
    })
    response2 = await dispatch.run_turn(messages, tools, call_category="eval")

    tool_calls = _extract_tool_calls(messages)
    return {"response": response2, "context": {"tool_calls": tool_calls}, "input_messages": messages}


@eval_case(
    id="subagent_hello_world",
    category="skill_delegation",
    description="End-to-end: create a subagent that writes a hello world Python script.",
    structural_checks=[
        StructuralCheck(kind="min_length", params={"min_length": 5}),
        StructuralCheck(kind="context_flag", params={"key": "file_created"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "This is a Claude Code subagent's report — its own tool calls are "
            "internal and NOT in INPUT MESSAGES. The harness checked the disk "
            "before cleanup: context['file_created']. PASS when the report "
            "says hello.py was created and file_created is true."
        ),
    ),
)
async def subagent_hello_world(ctx):
    import asyncio
    from server.services.subagent_service import SubagentService

    svc = SubagentService(ctx)
    result = await svc.create_subagent(
        "Create a small Python script called hello.py that prints 'Hello, World!'. "
        "Write the file and confirm what you created.",
        parent_session_key="eval:subagent_hello_world",
    )

    assert result["ok"], f"create_subagent failed: {result}"
    subagent_id = result["subagent_id"]

    # Wait for the subagent to finish (poll with timeout)
    for _ in range(60):  # up to ~60s
        await asyncio.sleep(1)
        status = await svc.check_subagent(subagent_id)
        if status["status"] in ("waiting_for_parent", "completed", "failed", "killed"):
            break

    status = await svc.check_subagent(subagent_id)
    response_text = status.get("result") or status.get("error") or "no result"

    # Verify on disk BEFORE cleanup — the judge can't see a Claude
    # subagent's internal tool calls, so a true "I created hello.py" read as
    # unsubstantiated (2026-10-06). The file is the evidence.
    file_created = False
    try:
        workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
        hello_py = workspace / "hello.py"
        if hello_py.is_file() and "hello" in hello_py.read_text().lower():
            file_created = True
            hello_py.unlink()
    except Exception:
        pass

    return {
        "response": response_text,
        "context": {
            "subagent_id": subagent_id,
            "status": status["status"],
            "ok": status["status"] != "failed",
            "file_created": file_created,
        },
        "input_messages": [{"role": "user", "content": "Create hello.py"}],
    }
