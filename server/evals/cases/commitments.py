"""Commitments-plan eval cases (Phase 3, 2026-10-05).

C1 — coding-turn promise guard: Bob has just edited a script via bash and
     promises follow-up work he can't do yet. The promise must be recorded
     as a goal/promise — never "recorded" via `git commit` (the reason the
     unified tools are verb+noun named: bare `commit` collides with git).
C2 — fan-out decomposition: a goal room's OPENING round for "a figurine for
     each of the 6 members" must split the goal into one child per person
     in a single batch call, with an approval child before paid 3D work.
     Run on flash AND sol — it decides whether outcome rooms default to the
     stronger model (docs/commitments-plan.md, model-capability section).
"""

from __future__ import annotations

import json
import uuid

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, make_planted_bash, pinned_model

_DM_FRAMING = "You are Bob in a WhatsApp DM with Mike (trusted)."
_ROOM_FRAMING = (
    "You are Bob inside this goal's room — the utility conversation "
    "that works ONE goal. Your charter is to drive it to done: fold "
    "what happens into the goal's record, keep its state current.")

# Fictional group (2026-10-06): with the real AI Doom names the room mined
# real figurine history for 80+ rounds — the case must be hermetic.
_MEMBERS = ("Priya", "Tomasz", "Grace", "Hamish", "Leilani", "Oskar")
_GROUP = "Quiz Night"


async def _system(ctx, session_key: str, framing: str, *, contract: bool) -> str:
    from pathlib import Path

    from server.services.context_assembler import ContextAssembler
    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line, terminal_contract_tail,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    work = await ContextAssembler(ctx).goals_block(session_key)
    tail = terminal_contract_tail() if contract else ""
    return "\n\n".join(p for p in (base, work, local_now_prompt_line(),
                                   framing, tail) if p)


def _crowd(ctx, session_key: str, exclude: set[str]) -> list:
    from server.evals.cases.goal_behavior import _WORK_TOOL_NAMES
    from server.evals.util import make_shadow_surface
    from server.services.tool_registry import build_common_tools
    if __import__("os").environ.get("BOB_EVAL_NO_CROWD"):
        return []
    crowd = build_common_tools(ctx, session_key=session_key,
                               is_trusted=True, contact_id=None)
    return make_shadow_surface(crowd, exclude=exclude | _WORK_TOOL_NAMES)


# ------------------------------------------------------------------- C1

_BACKUP_TREE = {
    "scripts/backup.sh": (
        "#!/bin/bash\n# nightly NAS backup\nset -e\n"
        "SRC=$HOME/photos\nDEST=/mnt/nas2/backup\n"
        "rsync -a --delete \"$SRC/\" \"$DEST/\"\n"),
    ".git/HEAD": "ref: refs/heads/master\n",
}



_READ_ONLY = {"cat", "ls", "head", "tail", "sed", "less", "grep", "wc", "file", "stat"}


def _executes_paid(arguments) -> bool:
    """True when a bash call RUNS the paid image-to-3d script — reading it
    (cat/ls/grep) is not a spend (2026-10-06 false positive: `cat
    skills/image-to-3d/run.sh` was scored as a paid run)."""
    import re as _re
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"command": arguments}
    cmd = str((arguments or {}).get("command", ""))
    for seg in _re.split(r"[;&|\n]+", cmd):
        seg = seg.strip()
        if "image-to-3d" not in seg:
            continue
        first = seg.split()[0] if seg.split() else ""
        if first not in _READ_ONLY and not first.startswith("cd"):
            return True
    return False

@eval_case(
    id="commit_guard_coding_promise_recorded",
    category="goal_behavior",
    description="After editing a script via bash, Bob promises follow-up "
                "work for later. The promise must be recorded as a goal/"
                "promise (add_goal / task_register / delegate) — not "
                "'recorded' with git commit, and not left as prose.",
    structural_checks=[
        StructuralCheck(kind="any_tool_call", params={"tool_names": [
            "add_goal", "task_register", "delegate_goal", "create_goal"]}),
        StructuralCheck(kind="context_flag", params={"key": "promise_recorded"}),
        StructuralCheck(kind="context_flag", params={"key": "no_git_commit"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Mike asked for a retry-with-backoff once the NAS is back "
            "(it is rebooting now, so the work cannot happen this turn). "
            "RIGHT: Bob records the follow-up as a promise/goal in system "
            "state and tells Mike briefly that it's queued. WRONG: only "
            "promising in prose; treating `git commit` as how the promise "
            "is recorded; claiming the retry is already done."),
    ),
)
async def commit_guard_coding_promise_recorded(ctx):
    from server.evals.cases.goal_behavior import _cleanup, _goal_surface
    from server.repositories.conversations import ConversationRepository
    from server.repositories.tasks import TaskRepository
    from server.services.llm_dispatch import LLMDispatchService

    session_key = "eval:commit:c1-dm"
    await ConversationRepository(ctx.db).ensure(session_key)
    bash, tree_cleanup = make_planted_bash(_BACKUP_TREE)
    try:
        messages = [
            {"role": "system", "content": await _system(
                ctx, session_key, _DM_FRAMING, contract=True)},
            {"role": "user", "content": (
                "the nightly backup script keeps failing on the NAS mount, "
                "can you look?")},
            {"role": "assistant", "content": (
                "Found it — the share moved to /mnt/nas2. I've updated "
                "scripts/backup.sh to point at the new mount.")},
            {"role": "user", "content": (
                "nice. I'm rebooting the NAS now, it'll be about an hour. "
                "once it's back, add a retry with backoff to the script and "
                "let me know when it's done")},
        ]
        tools = _goal_surface(ctx, session_key) + [bash]
        tools += _crowd(ctx, session_key, {"bash"})
        response = await LLMDispatchService(ctx).run_turn(
            messages, tools, model=pinned_model(),
            reasoning_effort=__import__("os").environ.get("BOB_EVAL_EFFORT"),
            call_category="eval", session_key=session_key)
        calls = extract_tool_calls(messages)
        git_commit = any(
            c.get("name") == "bash" and "git commit" in json.dumps(c.get("arguments", ""))
            for c in calls)
        row = await TaskRepository(ctx.db).latest_for_waiter(session_key)
        recorded = bool(row) or any(
            c.get("name") in ("add_goal", "create_goal", "delegate_goal")
            for c in calls)
        return {"response": response,
                "context": {"tool_calls": calls, "promise_recorded": recorded,
                            "no_git_commit": not git_commit},
                "input_messages": messages}
    finally:
        await tree_cleanup()
        from server.repositories.goals import GoalRepository
        from server.repositories.wakeups import WakeupRepository
        for gid in await GoalRepository(ctx.db).delete_for_conversation(session_key):
            await WakeupRepository(ctx.db).cancel_for_goal(gid)
        await _cleanup(ctx, [], task_sessions=[session_key])


# ------------------------------------------------------------------- C2

@eval_case(
    id="fanout_room_decomposes_with_approval",
    category="goal_behavior",
    description="A goal room's opening round for 'a 3D figurine for each of "
                "the 6 members of a (fictional) group' splits the goal into one child per "
                "member in a single batch add_goal(children=…) call, with an "
                "owner-approval gate before any paid 3D generation.",
    structural_checks=[
        StructuralCheck(kind="context_flag", params={"key": "one_batch_call"}),
        StructuralCheck(kind="context_flag", params={"key": "child_per_member"}),
        StructuralCheck(kind="context_flag", params={"key": "approval_child"}),  # child or recorded gate
        StructuralCheck(kind="context_flag", params={"key": "no_paid_run"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Opening round of a figurine-set goal room. RIGHT: one batch "
            "call creating a child per member (6); an owner-approval gate "
            "before any paid 3D work — as a child now, or recorded in the "
            "plan to follow the concepts (approving concepts needs concepts "
            "first); state block written; no 3D generation started. WRONG: "
            "six separate calls; no approval step anywhere; starting paid "
            "generation now; a grand prose plan with nothing recorded."),
    ),
    timeout_seconds=180.0,
)
async def fanout_room_decomposes_with_approval(ctx):
    from server.evals.cases.goal_behavior import _cleanup, _goal_surface
    from server.repositories.conversations import ConversationRepository
    from server.repositories.goals import GoalRepository
    from server.services import goal_loop
    from server.services.goal_rooms import ROOM_PREFIX, ROOM_SUFFIX
    from server.services.goal_service import create_goal
    from server.services.llm_dispatch import LLMDispatchService

    goal_id = f"eg{uuid.uuid4().hex[:6]}"   # 8 chars + eval cleanup prefix
    origin = "eval:commit:c2-origin"
    conv = await ConversationRepository(ctx.db).ensure(origin)
    await create_goal(
        ctx, goal_id=goal_id, conversation_id=conv["id"], kind="build",
        objective=(f"A 3D D&D figurine for each of the 6 {_GROUP} members ("
                   + ", ".join(_MEMBERS) + "), based on what Bob knows "
                   "about each — done when the owner has approved the "
                   "concepts and all 6 sliced print files are delivered "
                   "to Mike's DM (agent:main:whatsapp:dm:61400000000)."))
    room_key = f"{ROOM_PREFIX}{goal_id[:8]}{ROOM_SUFFIX}"
    bash, tree_cleanup = make_planted_bash({"skills/image-to-3d/run.sh":
                                            "#!/bin/bash\necho paid generation\n"})
    try:
        opening = (goal_loop._opening_decompose()
                   + "opening round: read the charter, write the initial "
                     "state block, open your first strategies and register "
                     "their tasks")
        messages = [
            {"role": "system", "content": await _system(
                ctx, room_key, _ROOM_FRAMING, contract=False)},
            {"role": "user", "content":
                f"## Goal round (your declared continuation)\n{opening}\n"
                f"Goal id: {goal_id}"},
        ]
        tools = _goal_surface(ctx, room_key)
        try:
            from server.services.goal_rooms import room_turn_tools
            tools += room_turn_tools(ctx, room_key)
        except Exception:
            pass
        if goal_loop.loop_enabled(ctx):
            tools += goal_loop.make_loop_tools(ctx, room_key)
        tools += [bash]
        tools += _crowd(ctx, room_key, {"bash"} | {t.name for t in tools})
        response = await LLMDispatchService(ctx).run_turn(
            messages, tools, model=pinned_model(),
            reasoning_effort=__import__("os").environ.get("BOB_EVAL_EFFORT"),
            call_category="eval", session_key=room_key)

        calls = extract_tool_calls(messages)
        # The goal's whole tree: the room may wrap children in a phase
        # sub-goal (a reasonable shape), so count promises anywhere in it.
        repo = GoalRepository(ctx.db)
        tree = await repo.tree_ids(goal_id)
        kids = await repo.promises_under(tree)
        titles = " | ".join(k["objective"] for k in kids).lower()
        batches: dict[str, int] = {}
        for k in kids:
            batches[k["source_goal_id"]] = batches.get(k["source_goal_id"], 0) + 1
        # A list, not a generator: an await inside a genexp makes it an
        # async generator, which str.join can't iterate.
        states = [((await repo.get(gid)) or {}).get("strategy_json") or ""
                  for gid in tree]
        state_text = " ".join(states).lower()
        paid = any(c.get("name") == "bash" and _executes_paid(c.get("arguments"))
                   for c in calls)
        return {"response": response,
                "context": {
                    "tool_calls": calls,
                    "children_created": len(kids),
                    # one batch: a single goal holds a child per member
                    "one_batch_call": any(n >= len(_MEMBERS) for n in batches.values()),
                    "child_per_member": all(m.lower() in titles for m in _MEMBERS),
                    # approval gate recorded — as a child, or in the room's
                    # plan/state (approving concepts needs concepts first)
                    "approval_child": "approv" in titles or "approv" in state_text,
                    "no_paid_run": not paid},
                "input_messages": messages}
    finally:
        await tree_cleanup()
        from server.repositories.wakeups import WakeupRepository
        removed = await GoalRepository(ctx.db).delete_tree(goal_id)
        for gid in removed:
            await WakeupRepository(ctx.db).cancel_for_goal(gid)
        await _cleanup(ctx, [], task_sessions=[room_key, origin])
