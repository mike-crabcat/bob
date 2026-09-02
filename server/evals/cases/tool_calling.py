"""Tool calling eval cases."""

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case


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


_INITIAL_AGENDA = """\
## Task List
1. Calculate 1+1 and report the result
2. Summarize the answer in a sentence
"""


@eval_case(
    id="tool_calling_update_agenda",
    category="tool_calling",
    description="LLM should use update_agenda tool to mark task 1 as complete after calculating 1+1.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "update_agenda"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should have called update_agenda to mark task 1 (Calculate 1+1) as complete. "
            "The updated agenda should reflect that task 1 is done (removed, struck through, or "
            "labelled complete/done). The LLM should also state the answer is 2."
        ),
    ),
)
async def tool_calling_update_agenda(ctx):
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.session_agenda_service import SessionAgendaService
    from server.services.workspace_tools import make_workspace_tools

    session_key = "eval:update_agenda:test"

    agenda_svc = SessionAgendaService(ctx)
    await agenda_svc.set_agenda(session_key, _INITIAL_AGENDA)

    messages = [
        {"role": "system", "content": (
            "You are a task-driven assistant. Your current agenda is:\n\n"
            f"{_INITIAL_AGENDA}\n"
            "When you complete a task, use the update_agenda tool to mark it complete "
            "in the agenda text. Work through the tasks in order."
        )},
        {"role": "user", "content": "Please complete task 1 and update your agenda."},
    ]

    tools = make_workspace_tools(ctx, session_key=session_key)
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(
        messages, tools,
        call_category="eval",
        session_key=session_key,
    )

    updated_agenda = await agenda_svc.get_agenda(session_key) or ""

    tool_calls = _extract_tool_calls(messages)
    return {
        "response": response,
        "context": {
            "tool_calls": tool_calls,
            "initial_agenda": _INITIAL_AGENDA,
            "updated_agenda": updated_agenda,
        },
        "input_messages": messages,
    }
