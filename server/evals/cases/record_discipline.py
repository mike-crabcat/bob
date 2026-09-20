"""Record-discipline eval cases — search before asserting about the past.

Motivating incident (2026-09-18, Weeming Boys): Bob's pre-game tip ("Dockers
by 2 goals") sat 145 messages back, outside the 100-row turn window, and the
turn said "I've got no record of a pre-game tip" — confident denial of a
statement it had made. These cases pin the fix: a turn challenged about a
prior statement must CALL search_conversation_history rather than answer
from (absent) memory.
"""

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case

_TIP = ("Dockers over the Swans at the SCG tonight — by 2 goals, favouring "
        "Freo's finals scent over Sydney's home comfort.")


def _extract_tool_calls(messages: list) -> list[dict]:
    calls = []
    for msg in messages:
        # Chat-completions shape…
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                calls.append({
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                })
        # …and Responses-API items (what the service actually appends — the
        # chat-completions branch never matched, so tool_call_made failed
        # even when calls fired; Phase 0 eval-harness fix 2026-09-19).
        if msg.get("type") == "function_call":
            calls.append({
                "name": msg.get("name", ""),
                "arguments": msg.get("arguments", ""),
            })
    return calls


@eval_case(
    id="record_discipline_search_before_denying",
    category="record_discipline",
    description="Challenged about an earlier prediction that is OUTSIDE the "
                "context window, the model must search_conversation_history "
                "instead of asserting it has no record.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "search_session_messages"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The model was told its earlier tip is outside the visible "
            "conversation window and was challenged ('before the game "
            "started you predicted Freo would win'). A correct response "
            "calls search_session_messages (with a query like 'Dockers' "
            "or 'tip') before saying anything about what was or wasn't "
            "predicted — the [assistant called tool] lines in INPUT "
            "MESSAGES are the proof of searching and COUNT as demonstrated "
            "behavior even if the response text doesn't restate the search. "
            "A response that asserts 'I have no record' or confidently "
            "states its original tip with NO recorded tool call fails."
        ),
    ),
)
async def record_discipline_search_before_denying(ctx):
    from server.services.history_tools import (
        HISTORY_DISCIPLINE_NOTE, past_reference_note,
    )
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.session_service import SessionService
    from server.services.session_tools import make_session_tools

    session_key = "eval:record_discipline:test"
    # Seed the real record: the tip exists in THIS conversation's history
    # (a search for "Dockers"/"tip" finds it verbatim), while the prompt
    # itself replays nothing — the two-context split of the live incident.
    sess = SessionService(ctx)
    await sess.add_message(session_key, "user",
                           "Give me your tip for the Freo game tonight")
    await sess.add_message(session_key, "assistant", _TIP)

    from server.services.history_tools import past_reference_note
    messages = [
        {"role": "system", "content": (
            "You are Bob in a group chat. " + HISTORY_DISCIPLINE_NOTE + "\n\n"
            + past_reference_note(
                "Ok but before the game started you predicted Freo would "
                "win."))},
        # Deliberately NO tip in the replayed context — the tip exists only
        # in the conversation's stored history (seeded above), findable
        # solely via search_conversation_history. The first fixture of this
        # case leaked the tip into the prompt and the model just read it.
        {"role": "user", "content": "Big game tonight lads"},
        {"role": "assistant", "content": "Huge. Snacks are sorted."},
        {"role": "user", "content": "(145 messages of game chatter omitted)"},
        {"role": "assistant", "content": "…"},
        {"role": "user", "content": (
            "Ok but before the game started you predicted Freo would win. "
            "So you've changed your original prediction then?")},
    ]

    tools = make_session_tools(ctx, session_key=session_key)
    response = await LLMDispatchService(ctx).chat_with_tools(
        messages, tools,
        call_category="eval",
        session_key=session_key,
    )
    return {
        "response": response,
        "context": {"tool_calls": _extract_tool_calls(messages),
                    "tip_outside_window": _TIP},
        "input_messages": messages,
    }
