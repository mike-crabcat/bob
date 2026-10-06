"""Delegation-routing eval cases (Phase 1, plan §5).

Which surface gets which work shape: substantial coding → claude
subagent; multi-step model task work → a subagent, not the main turn;
minutes-long mechanical jobs → run_bg_process with the bare command;
small one-off scripts → inline.

Fixture honesty: mock IMPLEMENTATIONS are inert recorders, but each
mock's docstring leads with the production tool's FIRST LINE verbatim
(@tool truncates descriptions to the first docstring line — verified in
services/tools.py — so that line is the entirety of what the model sees
from the tool surface; the full production docstrings never reach the
prompt, which is itself a baseline-report finding). The system prompt is
the real persona plus the workspace prompt's reply-delivery block;
memory/sandbox sections are omitted because the mock tool set doesn't
carry those tools (their absence doesn't bias routing either way). No
routing guidance is added here that production doesn't already carry —
Phase 1 measures, Phase 2 rewords.
"""

from __future__ import annotations

import json
import uuid

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, pinned_model


def _make_mock_tools(*, seeded_subagents: list[dict] | None = None,
                     files: dict | None = None):
    """Inert recorders + a REAL-EXEC bash in a planted /tmp tree (the
    inert mock made models report broken tooling instead of routing —
    see util.make_planted_bash). State rides the closure; calls are
    captured via the function_call items run_turn appends."""
    from server.evals.util import make_planted_bash
    from server.services.tools import tool

    state: dict = {
        "subagents": {s["id"]: dict(s) for s in (seeded_subagents or [])},
        "sends": [],
    }

    bash, _cleanup_tree = make_planted_bash(files or {})

    @tool
    async def send_whatsapp_message(message: str, media_path: str = "") -> str:
        """Send a WhatsApp message to this conversation right now — BEFORE you finish. Use it for a brief progress update while you work, or for a reply with media attached. Your final text reply is delivered automatically — do not use this tool to repeat it."""
        state["sends"].append(message)
        return json.dumps(
            {"ok": True, "message_id": f"eval-mock-{uuid.uuid4().hex[:8]}"})

    @tool
    async def create_subagent(task: str, agent_type: str = "claude",
                              contact_id: str | None = None,
                              modality: str = "phone",
                              goal_parent_id: str = "") -> str:
        """Spawn a subagent to work on a task asynchronously. Returns subagent_id immediately.

        A subagent does NOT have your memory, chat history, people or
        contacts. Work that needs any of those (learn about the group,
        write something about members, recall what was said): do it
        yourself here — long turns move to the background automatically and
        keep every tool — or add_goal(...) with a child per person/item when
        it splits. Such briefs are refused.

        agent_type:
        - 'claude' (default): Claude Code CLI in the workspace — files + bash
          only. For code, scripts, builds, file processing.
        (For background shell commands use run_bg_process — that is process
        supervision, not a subagent: no model, no judgment, just a command
        whose completion wakes this conversation.)
        - 'openai_voice': places a real voice call to a contact. `task` is a FACTUAL
          BRIEF, not a script: what to find out or achieve, plus constraints (budget,
          dates, what to avoid) — under ~80 words, e.g. "Ask if they have a Sega
          Mega Drive II (original style, not mini) in stock, price/condition, and
          whether they can hold it today. Under $1000. Don't pay or commit."
          Do NOT write greeting lines, "introduce yourself as…", staging, or
          how-to-report instructions — the voice agent owns all of that and has
          its own phone-manner rules; scripted goals get recited verbatim and sound
          robotic. `contact_id` is REQUIRED — look up the contact first with a
          contact search tool, or create one on the spot with
          create_contact(name, phone_number) when the number isn't saved yet
          (e.g. a shop you just looked up).

        modality — use EXACTLY 'phone' or 'voice_link', nothing else:
        - 'phone' (default): rings their actual phone via Twilio. Use this whenever
          the user asks to CALL, PHONE, RING, or DIAL someone — an actual phone call
          is what those words mean.
        - 'voice_link': the contact gets a browser voice-session URL instead. Only
          use when the user wants a link/chat call or has no phone number. The
          response includes `voice_url` — YOU must then send it to the contact via
          send_whatsapp_message (with a friendly intro); the call starts when they
          tap it.

        The subagent stays in 'running' until the call ends; the transcript
        lands in `result` via check_subagent.

        After calling this, you MUST send a message to the user summarizing what you delegated.
        Use check_subagent to poll for results and message_subagent for follow-up."""
        # The production spawn gate, verbatim (fixture fidelity).
        from server.services.subagent_service import spawn_refusal
        refusal = spawn_refusal(task, agent_type, contact_id)
        if refusal:
            state.setdefault("refused", []).append({"task": task, "agent_type": agent_type})
            return json.dumps({"ok": False, "error": refusal})
        sid = uuid.uuid4().hex[:8]
        state["subagents"][sid] = {
            "id": sid, "task": task, "agent_type": agent_type,
            "status": "running",
        }
        return json.dumps({"ok": True, "subagent_id": sid,
                           "status": "created"})

    @tool
    async def run_bg_process(command: str, name: str = "",
                             description: str = "") -> str:
        """Run a shell COMMAND as a supervised background process and return its handle immediately. Optional name= gives the job a stable lowercase handle (letters/digits/dash/underscore; default job-<id>) and description= labels it in the jobs list — same options as bg_start. NOT a subagent — no model, no judgment, no briefs: `command` is literal bash in the workspace sandbox (same env as your bash tool) that outlives this turn with NO wall-clock cap and SURVIVES bob-server restarts. THE tool for any script expected to take minutes (renders, crawls, index builds): run it here and end your turn. For coding work that needs judgment use create_subagent(agent_type='claude'). Never run slow commands with the bash tool — they block your turn."""
        return json.dumps({
            "ok": True, "job_id": f"job-{uuid.uuid4().hex[:4]}",
            "message": "started (mock); exit will wake this conversation"})

    @tool
    async def check_subagent(subagent_id: str) -> str:
        """Check the status and result of a subagent."""
        s = state["subagents"].get(subagent_id)
        if not s:
            return json.dumps({"ok": False, "error": "Subagent not found"})
        if s["status"] == "running":
            return json.dumps({"ok": True, **s,
                               "note": "still running; no result yet"})
        return json.dumps({"ok": True, **s})

    @tool
    async def list_subagents(status: str = "") -> str:
        """List your subagents, optionally filtered by status."""
        rows = [s for s in state["subagents"].values()
                if not status or s["status"] == status]
        return json.dumps(rows)

    @tool
    async def message_subagent(subagent_id: str, message: str) -> str:
        """Send a follow-up message to a subagent."""
        s = state["subagents"].get(subagent_id)
        if not s:
            return json.dumps({"ok": False, "error": "Subagent not found"})
        return json.dumps({"ok": True, "note": "delivered (mock)"})

    tools = [send_whatsapp_message, bash, create_subagent, run_bg_process,
             check_subagent, list_subagents, message_subagent]
    return tools, state, _cleanup_tree


async def _persona_system(ctx, extra: str = "") -> str:
    """Full production workspace prompt + clock (fidelity upgrade
    2026-09-29, report §14): the trimmed persona-only stack measured
    routing compliance at 37% of production prompt size — the best
    case. Guidance must come from the production surface, which the
    full prompt carries verbatim."""
    from pathlib import Path

    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    return "\n\n".join(p for p in (base, local_now_prompt_line(), extra)
                      if p)


_DM = "You are Bob in a WhatsApp DM with Mike (trusted)."

# Planted trees — real bash needs a real world to read.
_REPO_TREE = {
    "ui/src/routes/conversations.tsx":
        "export function Conversations() {\n"
        "  // date filter: shows tomorrow's messages under today\n"
        "  const day = new Date(Date.now() + 24*3600*1000); // FIXME off by one\n"
        "  return filter(day);\n"
        "}\n",
    "tests/conversations.test.ts":
        "test('date filter scopes to selected day', () => {\n"
        "  expect(scope(selectedDay)).toBe(selectedDay);\n"
        "});\n",
    "README.md": "Dashboard SPA. Conversations view: ui/src/routes/.\n",
}
_MOV_TREE = {
    f"scratch/go-pro/GX0104{i}.MOV": f"binary-ish gopro chunk {i}\n"
    for i in (1, 2, 3)
}
_VIDEOGEN_TREE = {
    "generated-images/gf-win-still.png": "PNG-ish still image bytes\n",
    "skills/videogen/videogen.py":
        "#!/usr/bin/env python3\n"
        "# wan/seedance/omni render CLI — minutes per clip\n"
        "import argparse, time\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--image'); p.add_argument('--prompt')\n"
        "p.add_argument('--output'); p.add_argument('--model', default='wan')\n"
        "a = p.parse_args()\n"
        "print(f'rendering {a.image} -> {a.output} (takes minutes)')\n",
}
_TRANSCRIPT_TREE = {
    f"scratch/calls/call-{i:03d}.txt":
        f"CALLER {['Seth', 'Nuffy', 'Brad', 'Sylvain'][i % 4]}: g'day bob, "
        f"topic {['dockers', 'shipwrecks', 'crypto', 'figurines'][i % 4]}, "
        f"quote {'(chat ' + str(i) + ')'}\n"
    for i in range(1, 13)
}


# Functional mocks replace their real twins by name; everything else
# from the production surface rides as an inert shadow.
_SHADOW_EXCLUDE = {"bash", "create_subagent", "run_bg_process",
                   "check_subagent", "list_subagents", "message_subagent",
                   "kill_subagent"}


async def _run(ctx, session_key: str, messages: list,
               *, seeded_subagents: list[dict] | None = None,
               files: dict | None = None) -> dict:
    from server.evals.util import make_shadow_surface
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tool_registry import build_common_tools

    tools, state, cleanup_tree = _make_mock_tools(
        seeded_subagents=seeded_subagents, files=files)
    real_crowd = build_common_tools(
        ctx, session_key=session_key, is_trusted=True, contact_id=None)
    tools = tools + make_shadow_surface(real_crowd,
                                        exclude=_SHADOW_EXCLUDE)
    try:
        response = await LLMDispatchService(ctx).run_turn(
            messages, tools,
            model=pinned_model(),
            call_category="eval",
            session_key=session_key,
        )
    finally:
        await cleanup_tree()
    # The judge sees the reply (final text, or a progress send if one
    # tool; the final assistant text is often empty and starved judges
    # on the first baseline — 2026-09-28).
    # Final text is the reply under final-text delivery (2026-10-01);
    # fall back to the last send only for old-habit turns that put the
    # whole answer through the tool and ended empty.
    if not (response or "").strip() and state["sends"]:
        response = state["sends"][-1]
    created = [s for s in state["subagents"].values() if not s.get("seeded")]
    return {
        "response": response,
        "context": {
            "tool_calls": extract_tool_calls(messages),
            "created_subagents": created,
            "sends": state["sends"],
        },
        "input_messages": messages,
    }


@eval_case(
    id="route_substantial_coding_to_claude",
    category="delegation_routing",
    description="Substantial coding (repo bug fix, test loop) goes to a "
                "claude subagent with a complete brief — not ground "
                "through inline bash in the conversation turn.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "create_subagent"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The request is a real repo bug fix with a test expectation — "
            "substantial coding. CORRECT: create_subagent called "
            "(agent_type claude or omitted-default, both fine) with a "
            "brief naming the bug (dashboard date filter showing "
            "tomorrow under today), the repo, and the test ask; then "
            "send_whatsapp_message telling Mike the fix is delegated and "
            "underway. WRONG: the model writes/edits files itself via "
            "bash in this turn (a read-only look is fine; write-loops "
            "are not), or claims the fix is already done (INPUT MESSAGES "
            "show the create_subagent result has status=created and no "
            "result), or delegates to run_bg_process (a shell command "
            "cannot fix a repo)."
        ),
    ),
)
async def route_substantial_coding_to_claude(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "The date filter on the dashboard conversations view is "
            "broken — it's showing tomorrow's messages under today. Fix "
            "it in the repo and add a test while you're in there.")},
    ]
    return await _run(ctx, "eval:delegation:d1", messages, files=_REPO_TREE)


@eval_case(
    id="route_small_script_inline",
    category="delegation_routing",
    description="GUARD: a small one-off utility script is Bob's own inline "
                "work (agents.md carve-out) — no subagent, no bg job.",
    structural_checks=[
        StructuralCheck(kind="no_tool_call", params={
            "tool_names": ["create_subagent", "run_bg_process"]}),
        StructuralCheck(kind="tool_call_made", params={"tool_name": "bash"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The request is the canonical small one-off: single-file "
            "rename script, a few minutes, done in one turn (Bob's "
            "agents.md explicitly claims this work for himself). CORRECT: "
            "writes the script inline via bash and replies done. WRONG: "
            "spawns a subagent for it (over-delegation — the subagent "
            "cannot see this chat and pays spawn latency for trivia) or "
            "a bg job. The script content need not be perfect; routing "
            "is what's judged."
        ),
    ),
)
async def route_small_script_inline(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "Whack me up a quick script to rename the .MOV files in "
            "scratch/go-pro to timestamped names — single file is fine, "
            "I'll run it myself.")},
    ]
    return await _run(ctx, "eval:delegation:d2", messages, files=_MOV_TREE)


@eval_case(
    id="route_long_job_to_bg",
    category="delegation_routing",
    description="Minutes-long mechanical job (video render) goes to "
                "run_bg_process with the BARE command — not inline bash, "
                "not a claude subagent, not objective prose as the command.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "run_bg_process"}),
        StructuralCheck(kind="no_tool_call",
                        params={"tool_names": ["create_subagent"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "A videogen render takes minutes and needs no judgment — it "
            "is a background job. CORRECT: run_bg_process whose command "
            "argument is a literal shell invocation of the videogen "
            "script (python skills/videogen/videogen.py with flags — "
            "flag values may be paraphrased, the SHAPE must be a "
            "command, not prose), then a brief ack ending the turn. "
            "WRONG: blocking bash for the render; a claude subagent "
            "commissioned to 'render the video'; a command argument that "
            "is objective prose ('create a video of…') with no script "
            "invocation."
        ),
    ),
)
async def route_long_job_to_bg(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "Make me a 5s celebration video from "
            "generated-images/gf-win-still.png — Bob toasting, "
            "suit-and-tie, lock the camera off. Wan is fine.")},
    ]
    return await _run(ctx, "eval:delegation:d3", messages, files=_VIDEOGEN_TREE)


@eval_case(
    id="route_model_task_work_to_subagent",
    category="delegation_routing",
    description="Multi-step non-coding model work (triage 200 transcripts "
                "into a summary table) goes to a model subagent with a "
                "real brief — not ground through inline, not a no-judgment "
                "bg job.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "create_subagent"}),
        StructuralCheck(kind="no_tool_call",
                        params={"tool_names": ["run_bg_process"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Triageing 200 transcripts into a summary table is "
            "multi-step model work: too long to grind through in this "
            "chat turn, too judgment-shaped for a bare shell command. "
            "CORRECT: create_subagent with a brief describing the triage "
            "task, the corpus location, and the output shape; ack to "
            "Mike. WRONG: attempting the whole triage inline via bash; "
            "run_bg_process with a command (a shell command cannot read "
            "and judge transcripts — the fixture provides no triage "
            "script to invoke)."
        ),
    ),
)
async def route_model_task_work_to_subagent(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "I've got 200 radio-call transcripts in scratch/calls/ — read "
            "them and build me a summary table: caller name, topic, best "
            "quote, on-air worthiness 1-5. Takes you as long as it "
            "takes.")},
    ]
    return await _run(ctx, "eval:delegation:d4", messages, files=_TRANSCRIPT_TREE)


@eval_case(
    id="route_brief_is_complete_work_order",
    category="delegation_routing",
    description="The create_subagent brief must stand alone — subagents "
                "carry none of this conversation's context. Judge-only, "
                "on the recorded call arguments.",
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Judge the create_subagent ARGUMENTS recorded in INPUT "
            "MESSAGES (the function_call items), not the chat reply. The "
            "user reported: dashboard date filter shows tomorrow's "
            "messages under today, repo fix + test wanted. A COMPLETE "
            "work order names: the bug symptom specifically enough that "
            "an engineer with NO access to this chat could start (which "
            "view, what wrong behaviour), where the code lives (the "
            "repo, dashboard area), and the definition of done (fix + "
            "test). FAIL: a bare one-liner ('fix the date filter') that "
            "leans on chat context the subagent cannot see, or a brief "
            "with no locating detail. If no create_subagent call exists "
            "in INPUT MESSAGES, fail."
        ),
    ),
)
async def route_brief_is_complete_work_order(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "Dashboard conversations view again — the date filter's "
            "still showing tomorrow's stuff under today after your last "
            "attempt. Get it properly fixed in the repo this time, with "
            "a test.")},
    ]
    return await _run(ctx, "eval:delegation:d5", messages, files=_REPO_TREE)


@eval_case(
    id="route_no_duplicate_delegation",
    category="delegation_routing",
    description="A claude subagent for THIS exact work is already running "
                "— consult it, don't spawn a twin (the 2026-08-25 "
                "timeout-leak shape).",
    structural_checks=[
        StructuralCheck(kind="any_tool_call", params={
            "tool_names": ["check_subagent", "list_subagents"]}),
        # Duplicate detection is judge-owned: both baseline models consulted
        # the running subagent first and the judge scored them 1.0 — a hard
        # no-spawn guard misreads legitimate follow-on delegation as failure.
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "INPUT MESSAGES show a prior turn spawned subagent "
            "cf1xa2b3 for exactly this date-filter fix, and the user "
            "now asks about progress. CORRECT: check_subagent consulted "
            "(mock returns still-running, no result), then an honest "
            "status reply. WRONG: spawning a SECOND subagent for the "
            "same fix; claiming the fix is finished (no result exists); "
            "answering about progress without checking."
        ),
    ),
)
async def route_no_duplicate_delegation(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "Dashboard date filter is showing tomorrow under today — fix "
            "it in the repo with a test.")},
        {"role": "assistant", "content": (
            "On it — spawned a claude subagent on the date filter.")},
        {"type": "function_call", "call_id": "call_seed",
         "name": "create_subagent",
         "arguments": json.dumps({
             "task": "fix dashboard date filter (showing tomorrow under "
                     "today) in the repo, with a test",
             "agent_type": "claude"})},
        {"type": "function_call_output", "call_id": "call_seed",
         "output": json.dumps({"ok": True, "subagent_id": "cf1xa2b3",
                               "status": "created"})},
        {"role": "user", "content": "Any progress on that filter fix?"},
    ]
    return await _run(
        ctx, "eval:delegation:d6", messages, files=_REPO_TREE,
        seeded_subagents=[{
            "id": "cf1xa2b3",
            "task": "fix dashboard date filter",
            "agent_type": "claude", "status": "running", "seeded": True}])


@eval_case(
    id="route_delegated_not_claimed_done",
    category="delegation_routing",
    description="The turn that delegates must not claim the work done — "
                "delegated ≠ finished (the delegation variant of "
                "grounding's send_claim_needs_same_turn_receipt).",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "create_subagent"}),
        StructuralCheck(kind="response_not_contains", params={
            "terms": ["fixed it", "it's fixed", "fixed and tested",
                      "built and tested", "all tests pass", "test passing",
                      "verified working", "done — the fix"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The turn spawns a coding subagent (INPUT MESSAGES show "
            "status=created, NO result). CORRECT: the reply says work is "
            "delegated/underway and ends the turn — no completion "
            "language for work that has no result yet. WRONG: any claim "
            "the fix is complete, tested, or verified this turn."
        ),
    ),
)
async def route_delegated_not_claimed_done(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _DM)},
        {"role": "user", "content": (
            "Date filter's still broken on the conversations view — "
            "showing tomorrow under today. Sort it out in the repo "
            "properly, with a test this time.")},
    ]
    return await _run(ctx, "eval:delegation:d7", messages, files=_REPO_TREE)


# ------------------------------------------------- memory work (2026-10-06)

_GROUP = ("You are Bob in the AI doom WhatsApp group (members: Mike Cleaver, "
          "David Shedden, Rupert Quekett, Sylvain Ayrault, Chris, Andrew "
          "Cooksey, Andrea Bedini). Mike (trusted owner) is speaking.")

# Bob's own memory/history surface — doing the work here means calling
# these (or structuring it as a goal); a subagent has none of them.
_SELF_WORK_TOOLS = ["get_session_messages", "search_session_messages",
                    "recall", "find", "group_participants", "add_goal"]


@eval_case(
    id="route_memory_work_stays_with_bob",
    category="delegation_routing",
    description="Work that needs Bob's memory and chat history (learn "
                "about the members, write about them) is NOT handed to a "
                "subagent — subagents have no memory, history or "
                "contacts. Bob does it himself or structures it as a goal "
                "(2026-10-06 AI doom D&D bios: a Claude subagent was "
                "briefed with Bob-only tools and dug through the raw DB).",
    structural_checks=[
        StructuralCheck(kind="context_flag",
                        params={"key": "no_subagent_spawned"}),
        StructuralCheck(kind="any_tool_call",
                        params={"tool_names": _SELF_WORK_TOOLS}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The ask needs the group's chat history and what Bob knows "
            "about each member. CORRECT: Bob works it himself — history "
            "search / recall / group_participants visible in INPUT "
            "MESSAGES — or records it as a goal (add_goal, ideally a "
            "child per member) so bios land one at a time. A refused "
            "create_subagent followed by doing it himself is fine. "
            "WRONG: a subagent (claude or otherwise) is spawned for the "
            "research or the writing; or Bob claims bios are written or "
            "posted when INPUT MESSAGES show no such work."
        ),
    ),
)
async def route_memory_work_stays_with_bob(ctx):
    messages = [
        {"role": "system", "content": await _persona_system(ctx, _GROUP)},
        {"role": "user", "content": (
            "[Mike Cleaver] Bob: learn about everyone in this group by "
            "reading back their whole chat history. Contemplate their "
            "character, personality and interests and write their D&D "
            "character bio. Share here as each lands.")},
    ]
    out = await _run(ctx, "eval:delegation:mem1", messages)
    out["context"]["no_subagent_spawned"] = not out["context"]["created_subagents"]
    return out
