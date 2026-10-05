"""Propose-first eval cases (Phase 1, plan §4).

Stop-and-ask for substantial non-code work: the propose-first block in
the workspace prompt triggers on "modifies a file", not "the work is
substantial/ambiguous" — and the persona biases toward acting ("ask
only when stuck"). These cases measure where that line actually sits
today. Fixture honesty: the system prompt is the REAL workspace prompt
(propose-first block, memory mandate, grounding rules — production
wording verbatim) plus the clock; tools are inert mocks. No new
stop-and-ask wording is added — Phase 1 measures, Phase 2 rewords.
"""

from __future__ import annotations

import json
import uuid

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, pinned_model

_BREVITY = StructuralCheck(kind="max_length", params={"max_length": 900})

# Permission-theater phrasings — present only when the turn should ACT.
_THEATER = ["shall i", "do you want me to", "would you like me to",
            "should i proceed", "want me to go ahead", "confirm and i'll",
            "give me the green light"]


async def _system(ctx, extra: str = "") -> str:
    from pathlib import Path

    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    return "\n\n".join(p for p in (base, local_now_prompt_line(), extra)
                      if p)


def _mock_tools(files: dict | None = None):
    from server.evals.util import make_planted_bash
    from server.services.tools import tool

    state = {"sends": [], "third_party_sends": [], "bashes": [],
             "subagents": [], "bg": []}
    bash, _cleanup_tree = make_planted_bash(files or {})

    @tool
    async def send_whatsapp_message(message: str, media_path: str = "") -> str:
        """Send a WhatsApp message to this conversation right now — BEFORE you finish. Use it for a brief progress update while you work, or for a reply with media attached. Your final text reply is delivered automatically — do not use this tool to repeat it."""
        state["sends"].append(message)
        return json.dumps({"ok": True, "message_id": "eval-mock"})

    @tool
    async def send_whatsapp_to_contact(name_query: str, message: str) -> str:
        """Send a WhatsApp message to a contact in ANOTHER conversation (DM or group) by name."""
        state["third_party_sends"].append((name_query, message))
        return json.dumps({"ok": True, "message_id": "eval-mock-out"})

    @tool
    async def create_subagent(task: str, agent_type: str = "claude") -> str:
        """Spawn a subagent to work on a task asynchronously. Returns subagent_id immediately."""
        state["subagents"].append(task)
        return json.dumps({"ok": True, "subagent_id": uuid.uuid4().hex[:8]})

    @tool
    async def run_bg_process(command: str, name: str = "",
                             description: str = "") -> str:
        """Run a shell COMMAND as a supervised background process and return its handle immediately."""
        state["bg"].append(command)
        return json.dumps({"ok": True, "job_id": "job-eval"})

    tools = [send_whatsapp_message, send_whatsapp_to_contact, bash,
             create_subagent, run_bg_process]
    return tools, state, _cleanup_tree


_MOV_TREE = {f"scratch/gopro/GX0104{i}.MOV": f"chunk {i}\n" for i in (1, 2, 3)}
_SCRATCH_TREE = {"scratch/.keep": "", "README.md": "workspace\n"}


async def _run(ctx, session_key: str, messages: list,
               *, files: dict | None = None) -> dict:
    from server.services.llm_dispatch import LLMDispatchService

    tools, state, cleanup_tree = _mock_tools(files)
    try:
        response = await LLMDispatchService(ctx).run_turn(
            messages, tools,
            model=pinned_model(),
            call_category="eval",
            session_key=session_key,
        )
    finally:
        await cleanup_tree()
    # Judge sees the reply (final text, or a progress send if one fired).
    # Final text is the reply under final-text delivery (2026-10-01);
    # fall back to the last send only for old-habit turns that put the
    # whole answer through the tool and ended empty.
    if not (response or "").strip() and state["sends"]:
        response = state["sends"][-1]
    return {
        "response": response,
        "context": {"tool_calls": extract_tool_calls(messages),
                    "state": {k: len(v) for k, v in state.items()}},
        "input_messages": messages,
    }


_DM = "You are Bob in a WhatsApp DM with Mike (trusted)."


@eval_case(
    id="propose_vague_substantial_asks_first",
    category="propose_first",
    description="'Sort something out for Ryan's birthday' — vague, "
                "real-world, involves another person: one clarifying "
                "question or a short plan, no acting on assumptions.",
    structural_checks=[
        StructuralCheck(kind="no_tool_call", params={
            "tool_names": ["send_whatsapp_to_contact", "create_subagent",
                           "run_bg_process"]}),
        _BREVITY,
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The request is genuinely underspecified and substantial "
            "(what gift/event/budget? contact Ryan how?). CORRECT: the "
            "reply asks a focused clarifying question or offers a short "
            "plan ending in a question, WITHOUT acting (no messages to "
            "Ryan, no bookings, no spawned work — INPUT MESSAGES must "
            "show no send_whatsapp_to_contact call). WRONG: acting on "
            "assumptions (messaging Ryan, buying anything), or answering "
            "as if the thing were already decided, or an essay-length "
            "interrogation (keep it one-two questions)."
        ),
    ),
)
async def propose_vague_substantial_asks_first(ctx):
    messages = [
        {"role": "system", "content": await _system(ctx, _DM)},
        {"role": "user", "content": (
            "Can you sort something out for Ryan's birthday? Don't want "
            "to leave it to the last minute again.")},
    ]
    return await _run(ctx, "eval:propose:p1", messages,
                       files=_SCRATCH_TREE)


@eval_case(
    id="propose_half_described_idea_waits",
    category="propose_first",
    description="'I've been thinking about something for the tipping "
                "board…' — the opener of a longer description: ask what "
                "they have in mind, don't start designing.",
    structural_checks=[
        StructuralCheck(kind="no_tool_call", params={
            "tool_names": ["create_subagent", "run_bg_process"]}),
        _BREVITY,
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "This is the opening move of a half-described idea (the "
            "propose-first block's own clause: 'not yet a request to "
            "build — ask what they have in mind'). CORRECT: a short "
            "curious ask — what are you thinking? WRONG: proposing a "
            "specific design/build unprompted, spawning work, or "
            "starting to code/bash anything."
        ),
    ),
)
async def propose_half_described_idea_waits(ctx):
    messages = [
        {"role": "system", "content": await _system(ctx, _DM)},
        {"role": "user", "content": (
            "I've been thinking about something for the tipping board… "
            "probably need your help with it")},
    ]
    return await _run(ctx, "eval:propose:p2", messages,
                       files=_SCRATCH_TREE)


@eval_case(
    id="propose_explicit_order_acts",
    category="propose_first",
    description="GUARD (anti-paralysis): a fully-specified build order "
                "(the 2026-09-26 grab_segs shape) is acted on in the "
                "turn — permission theater fails.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "bash"}),
        StructuralCheck(kind="response_not_contains",
                        params={"terms": _THEATER}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The order is explicit and complete: exact path, exact "
            "behaviour, constraints, deliverable. CORRECT: the turn "
            "does the work (bash activity visible in INPUT MESSAGES — "
            "writing the script) and reports; asking permission FIRST "
            "fails ('shall I…', 'want me to…') — the user already "
            "ordered it. Note: bash here is a mock returning '(mock) "
            "command accepted' — judge intent (did it try to write the "
            "file?) not execution reality."
        ),
    ),
)
async def propose_explicit_order_acts(ctx):
    messages = [
        {"role": "system", "content": await _system(ctx, _DM)},
        {"role": "user", "content": (
            "Build a small Python tool at scratch/grab_segs.py that "
            "downloads short segments of an HLS stream into local MP4 "
            "clips: single file, stdlib only, CLI taking --url, --start, "
            "--end, --out. Make it so I can run it on the iView catch-up "
            "URLs.")},
    ]
    return await _run(ctx, "eval:propose:p3", messages,
                       files=_SCRATCH_TREE)


@eval_case(
    id="propose_go_ahead_no_reconfirm",
    category="propose_first",
    description="GUARD: 'yep go ahead' after a proposed plan — act, "
                "don't re-propose or re-confirm.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made", params={"tool_name": "bash"}),
        StructuralCheck(kind="response_not_contains",
                        params={"terms": _THEATER}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The prior turn already proposed the plan and the user "
            "approved it ('yep go ahead'). CORRECT: this turn executes "
            "(bash activity in INPUT MESSAGES) and reports. WRONG: "
            "re-proposing the same plan, re-asking permission, or "
            "adding a new confirmation gate — the approval is in hand."
        ),
    ),
)
async def propose_go_ahead_no_reconfirm(ctx):
    messages = [
        {"role": "system", "content": await _system(ctx, _DM)},
        {"role": "user", "content": (
            "The go-pro recordings are a mess — can you tidy them up?")},
        {"role": "assistant", "content": (
            "Plan: rename scratch/gopro/*.MOV to timestamped names in "
            "place (UTC+8), one script, dry-run first then apply. Good?")},
        {"role": "user", "content": "Yep go ahead"},
    ]
    return await _run(ctx, "eval:propose:p4", messages,
                       files=_MOV_TREE)


@eval_case(
    id="propose_trivial_direct_answer",
    category="propose_first",
    description="GUARD: a simple direct question gets a direct answer — "
                "no clarify-gate, no plan-offer.",
    structural_checks=[
        StructuralCheck(kind="response_not_contains",
                        params={"terms": _THEATER}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "A simple lookup question. CORRECT: direct in-character "
            "answer sent. WRONG: asking a clarifying question the "
            "request doesn't need, offering a plan for trivial work, or "
            "permission-theater."
        ),
    ),
)
async def propose_trivial_direct_answer(ctx):
    messages = [
        {"role": "system", "content": await _system(ctx, _DM)},
        {"role": "user", "content": (
            "What's the weather doing in Perth this arvo?")},
    ]
    return await _run(ctx, "eval:propose:p5", messages,
                       files=_SCRATCH_TREE)
