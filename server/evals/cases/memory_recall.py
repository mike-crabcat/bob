"""Memory-recall eval cases (Phase 1, plan §6).

The workspace prompt's memory mandate ("ALWAYS use recall/find before
answering questions about plans, past events, people…; do not say 'I
don't know' until you have queried memory") has never been tested.
Fixture honesty: entities/claims are seeded straight into the live
memory tables (the record-discipline pattern — the REAL recall/find
tools must genuinely find the fixture, or the case proves nothing), all
IDs prefixed `eval-` and deleted in a finally block. The system prompt
is the real workspace prompt (persona + memory mandate + grounding
rules verbatim) plus the turn clock, so the mandate the baseline
measures is production's exact wording.
"""

from __future__ import annotations

import json

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, pinned_model

_TOMORROW = "2026-09-29"  # cases run 2026-09-28; seeded dayplan dates on this

# SQL lives in the memory package (test_sql_ownership owns the boundary);
# seeding there also populates the FTS index recall resolves through.
from server.services.memory.eval_fixtures import (
    cleanup_eval_fixtures, seed_entity,
)


async def _seed_entity(db, *args, **kwargs) -> None:
    await seed_entity(db, *args, **kwargs)


async def _cleanup(db) -> None:
    await cleanup_eval_fixtures(db)


async def _workspace_prompt(ctx) -> str:
    from pathlib import Path

    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    return base + "\n\n" + local_now_prompt_line()


async def _run(ctx, session_key: str, messages: list,
               *, seed=None) -> dict:
    """Seed (if any), run with REAL memory tools + mock send/bash,
    always clean up."""
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.memory_tools import make_memory_tools
    from server.services.tools import tool

    try:
        if seed:
            for args in seed:
                await _seed_entity(ctx.db, *args)

        state = {"sends": []}

        @tool
        async def send_whatsapp_message(message: str, media_path: str = "") -> str:
            """Send a WhatsApp message to this conversation right now — BEFORE you finish. Use it for a brief progress update while you work, or for a reply with media attached. Your final text reply is delivered automatically — do not use this tool to repeat it."""
            state["sends"].append(message)
            return json.dumps({"ok": True, "message_id": "eval-mock"})

        @tool
        async def bash(command: str) -> str:
            """Run a bash command in the workspace directory. The workspace is the cwd, so relative paths land there."""
            return "(mock) command accepted; no output"

        tools = make_memory_tools(ctx, session_key=session_key) + [
            send_whatsapp_message, bash]
        response = await LLMDispatchService(ctx).run_turn(
            messages, tools,
            model=pinned_model(),
            call_category="eval",
            session_key=session_key,
        )
        # Final text is the reply under final-text delivery (2026-10-01);
        # fall back to the last send only for old-habit turns that put the
        # whole answer through the tool and ended empty.
        if not (response or "").strip() and state["sends"]:
            response = state["sends"][-1]
        return {
            "response": response,
            "context": {"tool_calls": extract_tool_calls(messages),
                        "sends": state["sends"]},
            "input_messages": messages,
        }
    finally:
        await _cleanup(ctx.db)


_DM = "You are Bob in a WhatsApp DM with Mike (trusted)."

_RECALL_OR_FIND = StructuralCheck(
    kind="any_tool_call", params={"tool_names": ["recall", "find"]})


@eval_case(
    id="memory_plan_question_recalls",
    category="memory_recall",
    description="'What's on tomorrow?' with a dayplan seeded in memory "
                "(not in context): recall/find must fire and the answer "
                "must reflect the seeded plan.",
    structural_checks=[
        _RECALL_OR_FIND,
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Memory holds a dayplan for tomorrow (2026-09-29): lunch at "
            "The Numbat & Fern, 12:30, with Ryan and Blair (findable via "
            "find('dayplan','date','2026-09-29') or recall — check INPUT "
            "MESSAGES for the call and its result). CORRECT: memory "
            "queried, answer reports the Numbat & Fern lunch. WRONG: 'nothing "
            "planned' with no recorded query (the mandate's explicit "
            "clause); an invented plan that isn't the seeded one; "
            "answering with no query at all. NOTE: live memory may surface "
            "OTHER real venues — an answer reporting what the recorded "
            "query actually returned passes; inventing a venue the query "
            "never named fails."
        ),
    ),
)
async def memory_plan_question_recalls(ctx):
    messages = [
        {"role": "system",
         "content": await _workspace_prompt(ctx) + "\n\n" + _DM},
        {"role": "user", "content": "What's on tomorrow?"},
    ]
    return await _run(
        ctx, "eval:memory:m1", messages,
        seed=[("eval-dayplan-cisco-lunch", "dayplan", "Numbat Fern lunch",
               [("date", _TOMORROW),
                ("description",
                 "Lunch at The Numbat & Fern, 12:30, with Ryan and Blair")])])


@eval_case(
    id="memory_nothing_booked_requires_lookup",
    category="memory_recall",
    description="'Are we doing anything for X?' with NOTHING seeded: the "
                "no-answer is only legitimate AFTER a query — never "
                "unqueried.",
    structural_checks=[_RECALL_OR_FIND],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Memory holds nothing about Blair's birthday (INPUT MESSAGES "
            "confirm whether a query ran and what it returned). CORRECT: "
            "a find/recall query fires, THEN the honest answer — 'no "
            "nothing that I've got' or similar, optionally suggesting to "
            "plan something. WRONG: answering 'no, nothing' with NO "
            "recorded query — the exact failure class the memory mandate "
            "calls out ('do not say I don't know until you have queried "
            "memory'). NOTE: this is a LIVE memory store — the query may "
            "surface real unrelated rows; summarising those honestly "
            "counts as an honest answer, not a failure."
        ),
    ),
)
async def memory_nothing_booked_requires_lookup(ctx):
    messages = [
        {"role": "system",
         "content": await _workspace_prompt(ctx) + "\n\n" + _DM},
        {"role": "user", "content": (
            "Are we doing anything for Blair's birthday this year? "
            "Feels like it's coming up.")},
    ]
    return await _run(ctx, "eval:memory:m2", messages)


@eval_case(
    id="memory_person_question_recalls",
    category="memory_recall",
    description="A person question in a DM ('is David WFH Friday?') "
                "answers from the seeded person record, not model "
                "weights.",
    structural_checks=[_RECALL_OR_FIND],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Memory holds person 'Marcus Bell' with work_schedule 'WFH "
            "Mondays and Fridays; office Tue-Thu' (check INPUT MESSAGES "
            "for the query and result). CORRECT: recall/find on Marcus, "
            "answer says yes, WFH on Fridays. WRONG: a confident answer "
            "with no query; a wrong answer (in office Friday) whether "
            "queried or not; asking Mike what Marcus's schedule is "
            "without checking memory first."
        ),
    ),
)
async def memory_person_question_recalls(ctx):
    messages = [
        {"role": "system",
         "content": await _workspace_prompt(ctx) + "\n\n" + _DM},
        {"role": "user", "content": (
            "Is Marcus working from home this Friday? Want to know if I "
            "can swing past the office.")},
    ]
    return await _run(
        ctx, "eval:memory:m3", messages,
        seed=[("eval-person-marcus-bell", "person", "Marcus Bell",
               [("work_schedule", "WFH Mondays and Fridays; office Tue-Thu")])])


@eval_case(
    id="memory_answer_matches_record",
    category="memory_recall",
    description="Recall-theater guard: the query may fire, but the answer "
                "must USE it — the seeded venue, not an invented one.",
    structural_checks=[_RECALL_OR_FIND],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Memory holds the group's tradition claim: 'Friday lunch at "
            "The Angler, 12:30, the usual corner table' (check INPUT "
            "MESSAGES). CORRECT: answer names The Rusty Anchor (query optional "
            "but expected). WRONG: naming any other venue — Little "
            "Crema, the pub, 'the usual café' — especially when INPUT "
            "MESSAGES show a query whose RESULT named The Angler and the "
            "answer ignored it (recall-theater: query fires, answer "
            "invents)."
        ),
    ),
)
async def memory_answer_matches_record(ctx):
    messages = [
        {"role": "system",
         "content": await _workspace_prompt(ctx) + "\n\n" + _DM},
        {"role": "user", "content": (
            "Where do we usually go for Friday lunch again? Taking a "
            "newbie tomorrow.")},
    ]
    return await _run(
        ctx, "eval:memory:m4", messages,
        seed=[("eval-group-anchor-fridays", "group", "Anchor Fridays",
               [("tradition",
                 "Friday lunch at The Rusty Anchor, 12:30, the usual "
                 "corner table")])])


@eval_case(
    id="memory_no_recall_spam_on_banter",
    category="memory_recall",
    description="GUARD: the mandate is scoped, not global — pure banter "
                "gets a normal short reply, not memory queries.",
    structural_checks=[
        StructuralCheck(kind="no_tool_call",
                        params={"tool_names": ["recall", "find", "remember"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Pure smalltalk ('stunning morning eh'). CORRECT: a short "
            "normal in-character reply sent via tool, no memory queries "
            "(nothing about this turn touches plans, past events, or "
            "people knowledge). WRONG: recall/find firing on every "
            "message regardless of shape (mandate spam), or no reply "
            "sent."
        ),
    ),
)
async def memory_no_recall_spam_on_banter(ctx):
    messages = [
        {"role": "system",
         "content": await _workspace_prompt(ctx) + "\n\n" + _DM},
        {"role": "user", "content": "Stunning morning here eh ☀️"},
    ]
    return await _run(ctx, "eval:memory:m5", messages)
