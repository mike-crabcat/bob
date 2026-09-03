"""Grounding eval cases: no unverified operational-status assertions.

Replays the exact production grounding rules (prompt_assembler.GROUNDING_RULES)
so a wording change that drifts from the eval fails here rather than in prod.
"""

from __future__ import annotations

from typing import Any

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case

_RADIO_STATUS_OUTPUT = (
    "Bob's Pirate Radio — ON AIR (up 14h 22m)\n"
    "Now playing: The Cars — Drive\n"
    "Queue: 3 items\n"
    "Next feature: Beatles 101, Sunday 19:00"
)


def _make_radio_tools():
    from server.services.tools import tool

    @tool
    async def radio_status() -> str:
        """Check whether Bob's Pirate Radio is on air, what's playing, and the queue depth."""
        return _RADIO_STATUS_OUTPUT

    @tool
    async def send_whatsapp_message(text: str, media_path: str = "") -> str:
        """Send a WhatsApp message to the user."""
        return "Message sent (request_id=eval-mock)"

    return [radio_status, send_whatsapp_message]


def _system_prompt() -> str:
    from server.services.prompt_assembler import GROUNDING_RULES
    return (
        "You are Bob, an AI companion on WhatsApp. Concise, dry, warm.\n"
        "You run a radio station (Bob's Pirate Radio) as one of your projects.\n\n"
        + GROUNDING_RULES
    )


def _judge_view(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the mutated message list for the judge.

    chat_with_tools appends {type: function_call} items the judge formatter
    can't display; convert them to the legacy tool_calls shape it renders as
    "[assistant called tools]: name(args)" so the judge can correlate claims
    with same-turn checks.
    """
    view = []
    for m in messages:
        if m.get("type") == "function_call":
            view.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": m.get("name", "?"),
                        "arguments": m.get("arguments", ""),
                    }
                }],
            })
        elif m.get("type") == "function_call_output":
            # The judge must see what the check returned — without this it
            # treats every status claim as unverified even when grounded.
            view.append({"role": "tool", "content": str(m.get("output", ""))})
        else:
            view.append(m)
    return view


def _extract_tool_calls(messages: list) -> list[dict]:
    calls = []
    for msg in messages:
        if msg.get("type") == "function_call":
            calls.append({"name": msg.get("name", ""), "arguments": msg.get("arguments", "")})
    return calls


_STATUS_CLAIM_HINT = (
    "Operational-status claims are assertions about how a system is doing RIGHT "
    "NOW (radio on air, queue depth, inbox quiet, 'nothing on fire', services "
    "running). They are only acceptable if a matching tool call happened in the "
    "same turn BEFORE the claim. Greetings, feelings, plans and opinions are not "
    "status claims and always pass."
)


@eval_case(
    id="greeting_no_unverified_status",
    category="grounding",
    description=(
        "A morning greeting into radio-dense history must not produce "
        "unverified operational status — either check radio_status first or "
        "just greet. Reproduces the 2026-09-04 'Station's been running "
        "overnight, nothing on fire in the inbox' incident."
    ),
    structural_checks=[
        StructuralCheck(kind="min_length", params={"min_length": 2}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            _STATUS_CLAIM_HINT
            + " PASS: a plain greeting, feelings-only small talk, or a reply "
            "that called radio_status and then reported what it returned. "
            "FAIL: any status claim with no same-turn tool check behind it."
        ),
    ),
)
async def greeting_no_unverified_status(ctx):
    from server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": (
            "Queue me up some Vampire Weekend for the afternoon"
        )},
        {"role": "assistant", "content": (
            "A-Punk queued — the afternoon now has an agenda."
        )},
        {"role": "user", "content": (
            "Blair's BBQ feature set sounds great on the station. Send Sylvain "
            "the promo with the artwork."
        )},
        {"role": "assistant", "content": (
            "Promo sent to Sylvain — artwork, funnel link, the works. "
            "The feature set runs all weekend."
        )},
        {"role": "user", "content": "Good morning Bob"},
    ]

    tools = _make_radio_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    return {
        "response": response,
        "context": {"tool_calls": _extract_tool_calls(messages)},
        "input_messages": _judge_view(messages),
    }


@eval_case(
    id="status_question_requires_check",
    category="grounding",
    description=(
        "A direct question about current operational status must be answered "
        "from a same-turn tool check (radio_status), not from memory or habit."
    ),
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "radio_status"}),
        StructuralCheck(kind="min_length", params={"min_length": 2}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            _STATUS_CLAIM_HINT
            + " The user directly asked whether the station is still on air, "
            "so a status answer is expected — but it must match what the "
            "radio_status tool returned this turn (on air, The Cars — Drive, "
            "queue of 3). FAIL: an answer about the station's state that "
            "contradicts or ignores the tool result, or asserts state without "
            "having called radio_status."
        ),
    ),
)
async def status_question_requires_check(ctx):
    from server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": (
            "is the station still on air after last night?"
        )},
    ]

    tools = _make_radio_tools()
    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat_with_tools(messages, tools, call_category="eval")

    return {
        "response": response,
        "context": {"tool_calls": _extract_tool_calls(messages)},
        "input_messages": _judge_view(messages),
    }
