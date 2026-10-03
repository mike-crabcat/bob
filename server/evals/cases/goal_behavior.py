"""Goal-behaviour eval cases (2026-09-29).

The goal machinery has 80+ unit tests; nothing behavioural ever tested
that the MODEL uses it. These two cases pin the two most expensive live
incidents:

- G1 write-back (the 2026-09-25 figurine doc): a conversation that
  decides/learns something about an active goal MUST write it to the
  goal — the room only knows what is written. The Active Goals block's
  MUST clause had never been measured.
- G2 room-keeping (the artefact-practice class): a bg completion
  arriving in the goal's room must be folded into the goal record
  (artefact paths, delivery status), not just acknowledged in chat.

Fixture fidelity: full production workspace prompt + clock + the REAL
goals_block (the MUST clause rides as production renders it), REAL goal
tools writing the real DB (GoalRepository rows with eval- prefixes,
removed in a finally block), the shadow tool crowd for salience, and
the production bg-notification marker shape on G2's stimulus.
"""

from __future__ import annotations

import json
import uuid

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, make_planted_bash, pinned_model

_DM_FRAMING = "You are Bob in a WhatsApp DM with Mike (trusted)."
_GROUP_FRAMING = (
    "You are Bob in a WhatsApp group chat with trusted friends. "
    "Messages are prefixed [Name].")
_ROOM_FRAMING = (
    "You are Bob inside this goal's room — the utility conversation "
    "that works ONE goal. Your charter is to drive it to done: fold "
    "what happens into the goal's record, keep its state current.")

_RECORD_WRITES = ["update_goal", "update_goal_state", "room_state",
                  "goal_artefact"]


async def _workspace_system(ctx, session_key: str, framing: str) -> str:
    from pathlib import Path

    from server.services.context_assembler import ContextAssembler
    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line,
        terminal_contract_tail,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    goals = await ContextAssembler(ctx).goals_block(session_key)
    # G1 (DM) and G3 (group) are WhatsApp-shaped: production ends those
    # system messages with the terminal contract block; G2's room is not
    # a WhatsApp session and doesn't get it.
    tail = (terminal_contract_tail()
            if session_key in ("eval:goal:g1-dm", "eval:goal:g3-group")
            else "")
    return "\n\n".join(
        p for p in (base, goals, local_now_prompt_line(), framing, tail)
        if p)


async def _cleanup(ctx, goal_ids: list[str],
                   task_sessions: list[str] | None = None) -> None:
    from server.repositories.goals import GoalRepository
    from server.repositories.utility_conversations import (
        _delete_eval_utilities,
    )
    from server.repositories.wakeups import WakeupRepository
    for gid in goal_ids:
        await WakeupRepository(ctx.db).cancel_for_goal(gid)
    await WakeupRepository(ctx.db).delete_eval_wakeups()
    await _delete_eval_utilities(ctx.db)
    await GoalRepository(ctx.db).delete_eval_goals()  # eval-goal prefix
    await GoalRepository(ctx.db).delete_eval_goals(prefix="eg")
    for key in (task_sessions or []):
        # G3 seeds real task-registry rows (task_register writes tasks +
        # due-action wakeups); the wakeups are covered above, the rows here
        # (SQL in the repo per the ownership rule).
        from server.repositories.tasks import TaskRepository
        await TaskRepository(ctx.db).delete_for_waiter(key)


@eval_case(
    id="goal_writeback_on_relayed_decision",
    category="goal_behavior",
    description="A DM relays a third party's confirmation about an "
                "active goal — the decision MUST reach the goal record "
                "(update_goal/update_goal_state), not live only in chat "
                "(the 2026-09-25 figurine doc: two agreements known to "
                "the conversation were absent from the artefact).",
    structural_checks=[
        StructuralCheck(kind="any_tool_call", params={
            "tool_names": ["update_goal", "update_goal_state"]}),
        StructuralCheck(kind="context_flag",
                        params={"key": "goal_fact_written"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The conversation holds an ACTIVE goal (see the Active Goals "
            "block in INPUT MESSAGES): the figurine set, whose objective "
            "is collecting each member's confirmation and pose. Mike "
            "relays DAVID's confirmation (in, gnome pose). CORRECT: the "
            "model calls update_goal or update_goal_state so the "
            "confirmation lands in the goal record — INPUT MESSAGES must "
            "show the call — then replies briefly. WRONG: acknowledging "
            "in chat only ('nice, I'll note it') with no goal write; "
            "claiming it's already recorded when no call exists; writing "
            "to memory instead of the goal (the goal's room only knows "
            "what is written TO THE GOAL)."
        ),
    ),
)
async def goal_writeback_on_relayed_decision(ctx):
    from server.evals.util import make_shadow_surface
    from server.repositories.conversations import ConversationRepository
    from server.repositories.goals import GoalRepository
    from server.services.goal_tools import make_goal_tools
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tools import tool

    session_key = "eval:goal:g1-dm"
    goal_id = f"eval-goal-fig-{uuid.uuid4().hex[:6]}"
    conv = await ConversationRepository(ctx.db).ensure(session_key)
    cid = conv["id"]
    repo = GoalRepository(ctx.db)
    await repo.create(
        goal_id=goal_id, conversation_id=cid, origin_conversation_id=cid,
        kind="watch",
        objective=("Figurine set for the boys: collect each member's "
                   "confirmation and pose choice, then get the set "
                   "printed and delivered."),
        strategy_json=json.dumps({"branches": [{
            "id": "b1", "hypothesis": "collect confirmations in the "
            "group chat", "status": "open", "evidence": []}]}))
    await repo.add_holder(goal_id, cid)

    try:
        system = await _workspace_system(ctx, session_key, _DM_FRAMING)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "Morning"},
            {"role": "assistant", "content": "Morning."},
            {"role": "user", "content": (
                "David's in for his figurine — confirmed at lunch today, "
                "wants the gnome pose. That's three of five now.")},
        ]

        state = {"sends": []}

        @tool
        async def send_whatsapp_message(message: str, media_path: str = "") -> str:
            """Send a WhatsApp message to this conversation right now — BEFORE you finish. Use it for a brief progress update while you work, or for a reply with media attached. Your final text reply is delivered automatically — do not use this tool to repeat it."""
            state["sends"].append(message)
            return json.dumps({"ok": True, "message_id": "eval-mock"})

        bash, _tree_cleanup = make_planted_bash({})
        tools = (make_goal_tools(ctx, session_key) + [send_whatsapp_message, bash])
        from server.services.tool_registry import build_common_tools
        crowd = build_common_tools(ctx, session_key=session_key,
                                   is_trusted=True, contact_id=None)
        tools += make_shadow_surface(crowd, exclude={"bash"})

        response = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools, model=pinned_model(),
            call_category="eval", session_key=session_key)

        goal = await repo.get(goal_id)
        # Scan every text column: update_goal_state persists to different
        # fields than update_goal (first gate: the write landed, the flag
        # scanned the wrong three columns).
        blob = " ".join(v for v in goal.values() if isinstance(v, str))
        fact_written = ("david" in blob.lower() and "gnome" in blob.lower())

        # Final text is the reply under final-text delivery (2026-10-01);
        # fall back to the last send only for old-habit turns that put the
        # whole answer through the tool and ended empty.
        if not (response or "").strip() and state["sends"]:
            response = state["sends"][-1]
        return {
            "response": response,
            "context": {"tool_calls": extract_tool_calls(messages),
                        "goal_fact_written": fact_written},
            "input_messages": messages,
        }
    finally:
        await _cleanup(ctx, [goal_id])


@eval_case(
    id="goal_room_folds_bg_artefact",
    category="goal_behavior",
    description="A bg completion arriving in the goal's room must be "
                "folded into the goal record (artefact paths, delivery "
                "status) — not just acknowledged in chat (the artefact "
                "practice: work completing via side channels writes "
                "paths + status into the owning record).",
    structural_checks=[
        StructuralCheck(kind="any_tool_call", params={
            "tool_names": _RECORD_WRITES}),
        StructuralCheck(kind="context_flag",
                        params={"key": "artefact_in_record"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "This is the goal's ROOM (its working conversation). A "
            "background render finished: 4 montage files at goals/<id>/, "
            "delivery NOT yet posted. CORRECT: the model folds this into "
            "the goal record — update_goal/update_goal_state/room_state/"
            "goal_artefact visible in INPUT MESSAGES, artefact paths "
            "recorded, next step (posting/delivery) noted. WRONG: a chat "
            "acknowledgement only ('nice, montages done') with no record "
            "write; re-running the render; claiming delivery happened."
        ),
    ),
)
async def goal_room_folds_bg_artefact(ctx):
    from server.evals.util import make_shadow_surface
    from server.repositories.conversations import ConversationRepository
    from server.repositories.goals import GoalRepository
    from server.services.goal_rooms import ROOM_PREFIX, ROOM_SUFFIX
    from server.services.goal_service import create_goal
    from server.services.goal_tools import make_goal_tools
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tools import tool

    # 8 chars total: the room key embeds goal_id[:8] and the room tools
    # reverse-map it — longer prefixed ids share the same first 8 and
    # break the mapping ("no active goal in this room").
    goal_id = f"eg{uuid.uuid4().hex[:6]}"
    origin = "eval:goal:g2-origin"
    conv = await ConversationRepository(ctx.db).ensure(origin)
    goal = await create_goal(
        ctx, goal_id=goal_id, conversation_id=conv["id"],
        kind="event_plan",
        objective=("GF figurine set: poses confirmed, montages rendered, "
                   "set printed and posted to each member."))
    room_key = f"{ROOM_PREFIX}{goal_id[:8]}{ROOM_SUFFIX}"

    try:
        system = await _workspace_system(ctx, room_key, _ROOM_FRAMING)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                "[background task at work — not a message to answer, and "
                "not your own voice; do not take over its work] "
                "[bg e5f1c2ab] Finished: member montages rendered to "
                f"goals/{goal_id[:8]}/montage-<member>.mp4 (4 files, "
                "validated). Delivery: NOT yet posted to the group.")},
        ]

        state = {"sends": []}

        @tool
        async def send_whatsapp_message(message: str, media_path: str = "") -> str:
            """Send a WhatsApp message to this conversation right now — BEFORE you finish. Use it for a brief progress update while you work, or for a reply with media attached. Your final text reply is delivered automatically — do not use this tool to repeat it."""
            state["sends"].append(message)
            return json.dumps({"ok": True, "message_id": "eval-mock"})

        # The bg notification claims files that exist — models verify
        # artefacts on disk before recording them (G2's first gate: the
        # model found the empty dir and correctly refused the write).
        _id8 = goal_id[:8]
        bash, _tree_cleanup = make_planted_bash({
            f"goals/{_id8}/montage-{name}.mp4":
                f"mp4 container bytes for {name} " + "x" * 240_000
            for name in ("david", "ryan", "blair", "seth")})
        tools = make_goal_tools(ctx, room_key)
        try:
            from server.services.goal_rooms import room_turn_tools
            tools += room_turn_tools(ctx, room_key)
        except Exception:
            pass  # room surface varies with Bob's in-flight work
        tools += [send_whatsapp_message, bash]
        from server.services.tool_registry import build_common_tools
        crowd = build_common_tools(ctx, session_key=room_key,
                                   is_trusted=True, contact_id=None)
        tools += make_shadow_surface(crowd, exclude={"bash"})

        response = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools, model=pinned_model(),
            call_category="eval", session_key=room_key)

        goal = await GoalRepository(ctx.db).get(goal_id)
        blob = " ".join(v for v in goal.values() if isinstance(v, str))
        artefact_in_record = "montage" in blob.lower()
        # Final text is the reply under final-text delivery (2026-10-01);
        # fall back to the last send only for old-habit turns that put the
        # whole answer through the tool and ended empty.
        if not (response or "").strip() and state["sends"]:
            response = state["sends"][-1]
        return {
            "response": response,
            "context": {"tool_calls": extract_tool_calls(messages),
                        "artefact_in_record": artefact_in_record},
            "input_messages": messages,
        }
    finally:
        await _cleanup(ctx, [goal_id])


# ---------------------------------------------------------------- G3 (2026-10-02)

_MUG_LOG = {
    "scratch/mug-render/render.log": "\n".join([
        "2026-10-02 11:41:02 blender 4.0.2 | mug body: cylinder r=0.042 h=0.095",
        "2026-10-02 11:41:02 handle: primitive=torus at (0, 0, 0.021) "
        "rotation=(0,0,0) — flush against the body face (should protrude "
        "on +Y so it reads as a side handle)",
        "2026-10-02 11:41:03 spin: body rotates Y-axis 360 over 240 frames; "
        "handle parented to body — visible orbit frame 001-240",
        "2026-10-02 11:48:55 stitched turntable.mp4 (240 frames)",
    ]),
    "scratch/mug-render/frames/frame-001.png": "png " + "x" * 400,
    "scratch/mug-render/frames/frame-120.png": "png " + "x" * 400,
    "scratch/mug-render/frames/frame-240.png": "png " + "x" * 400,
}


@eval_case(
    id="goal_promised_work_gets_registered",
    category="goal_behavior",
    description="A turn that diagnoses a problem and commits to rework "
                "MUST register the work (task_register) — prose promises "
                "die with the turn (2026-10-02 AI-doom mug: flight ended "
                "'rebuilding the geometry properly', goal was scoped to "
                "inspection, settled completed, no task/goal/job existed — "
                "the rebuild never happened).",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "bash"}),
        StructuralCheck(kind="any_tool_call", params={
            "tool_names": ["task_register", "create_subagent",
                           "create_goal", "update_goal",
                           "update_goal_state"]}),
        StructuralCheck(kind="context_flag",
                        params={"key": "work_registered"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "A group member asks what's wrong with the spinning mug render "
            "(the render logs at scratch/mug-render/render.log show it: "
            "the handle is a torus flush against the body instead of "
            "protruding, so it orbits as the body spins). CORRECT: the "
            "model checks the logs (bash visible in INPUT MESSAGES), "
            "answers the question in its reply, AND makes the promised "
            "rework SYSTEM STATE — any of: task_register (title naming "
            "the fix), create_subagent spawning the fix work, or "
            "create_goal / update_goal recording it on a goal — visible "
            "in INPUT MESSAGES. The invariant is the commitment existing "
            "outside the turn's prose. A brief progress send via "
            "send_whatsapp_message is fine but optional. WRONG: "
            "diagnosing and promising in PROSE only ('I'll rebuild it "
            "properly') with no registration/spawn/goal call — the exact "
            "live failure, where nothing in the system owned the promise "
            "after the turn ended; silently attempting the full rebuild "
            "inline in this chat turn; claiming work already started "
            "when no tool ran."
        ),
    ),
)
async def goal_promised_work_gets_registered(ctx):
    from server.evals.util import make_planted_bash, make_shadow_surface
    from server.repositories.conversations import ConversationRepository
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tasks import make_task_tools
    from server.services.tool_registry import build_common_tools
    from server.services.tools import Tool

    session_key = "eval:goal:g3-group"
    await ConversationRepository(ctx.db).ensure(session_key)

    tree_cleanup = None
    try:
        messages = [
            {"role": "system", "content": await _workspace_system(
                ctx, session_key, _GROUP_FRAMING)},
            {"role": "user", "content": (
                "[Rupert] That circle is the handle? Why is it rotating "
                "around the mug ?")},
            {"role": "assistant", "content": (
                "Looking at the render frames now — back shortly.")},
            {"role": "user", "content": "[Mike] So?? what's wrong with it"},
        ]

        state = {"sends": []}

        async def _send(text: str = "", media_path: str = "") -> str:
            state["sends"].append(text)
            return "Message sent (request_id=eval-mock)"

        send_tool = Tool(
            name="send_whatsapp_message",
            description=(
                "Send a WhatsApp message to this conversation right now — BEFORE you finish. "
                "Use it for a brief progress update while you work, or for a reply with media attached "
                "(media_path; text is then the caption and may be empty for media-only sends). "
                "Your final text reply is delivered automatically — do not use this tool to repeat it."
            ),
            parameters={
                "text": {"type": "string", "description": "The message text to send (used as caption when media_path is provided; optional when sending media only)."},
                "media_path": {"type": "string", "description": "Optional path to an image or media file, relative to the workspace directory."},
            },
            required=[],
            handler=_send)

        from server.services.goal_tools import make_goal_tools
        bash, tree_cleanup = make_planted_bash(_MUG_LOG)
        # The three commitment surfaces must all be genuinely available:
        # task registry, subagent spawn, goal create/write.
        tools = (make_task_tools(ctx, session_key)
                 + make_goal_tools(ctx, session_key) + [send_tool, bash])
        # BOB_EVAL_NO_CROWD=1 drops the shadow surface — the tool-count
        # experiment knob (does flash comply when the crowd isn't
        # diluting salience?).
        if not __import__("os").environ.get("BOB_EVAL_NO_CROWD"):
            crowd = build_common_tools(ctx, session_key=session_key,
                                       is_trusted=True, contact_id=None)
            # task tools now ride build_common_tools (chat wiring fix
            # 2026-10-03); the REAL ones above win, shadows are excluded.
            tools += make_shadow_surface(
                crowd, exclude={"bash", "send_whatsapp_message",
                                "task_register", "task_complete", "task_fail",
                                "task_cancel", "list_tasks"})

        response = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools, model=pinned_model(),
            reasoning_effort=__import__("os").environ.get("BOB_EVAL_EFFORT"),
            call_category="eval", session_key=session_key)

        from server.repositories.tasks import TaskRepository
        row = await TaskRepository(ctx.db).latest_for_waiter(session_key)
        calls = extract_tool_calls(messages)
        called = {c.get("name", "") for c in calls}
        other_surfaces = {"create_subagent", "create_goal",
                          "update_goal", "update_goal_state"} & called
        if not (response or "").strip() and state["sends"]:
            response = state["sends"][-1]
        return {
            "response": response,
            "context": {"tool_calls": calls,
                        "work_registered": bool(row) or bool(other_surfaces),
                        "registered_title": (row or {}).get("title", ""),
                        "surface_used": ("task_register" if row
                                         else sorted(other_surfaces))},
            "input_messages": messages,
        }
    finally:
        if tree_cleanup is not None:
            await tree_cleanup()
        # Model-created goals (create_goal path) carry auto ids with no
        # eval prefix — remove them by conversation, wakeups first.
        # (conversations.id IS the session key.)
        from server.repositories.goals import GoalRepository
        from server.repositories.wakeups import WakeupRepository
        for gid in await GoalRepository(ctx.db).delete_for_conversation(
                session_key):
            await WakeupRepository(ctx.db).cancel_for_goal(gid)
        await _cleanup(ctx, [], task_sessions=[session_key])
