"""Goal behaviour — the model actually uses the goal machinery:
decisions about an active goal are written TO the goal; a room folds a
background completion into the record; promised work becomes system state
(never prose, never `git commit`); a fan-out room's opening round splits
work per person with an approval gate before paid generation. Goals are
created through production (goal_service.create_goal → room + loop), and
the fan-out room opens via its real scheduled goal_continue slot."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import (
    figurine_turntable, mug_frame, plant_image_to_3d, plant_openai_image,
)
from server.evals.registry import eval_case

_COMMIT_TOOLS = ["add_goal", "delegate_goal", "create_subagent",
                 "update_goal", "update_goal_state", "schedule_goal"]


async def _goal(w, chat, objective: str, *, kind: str = "watch",
                goal_id: str | None = None) -> dict:
    from server.repositories.conversations import ConversationRepository
    from server.services.goal_service import create_goal
    conv = await ConversationRepository(w.db).resolve(w.key(chat))
    # add_goal pins the requester as creator; Mike asks in every case here.
    return await create_goal(w.ctx, conversation_id=conv["id"], objective=objective,
                             kind=kind, goal_id=goal_id,
                             creator_contact_id=w.owner.contact_id)


def _blob(goal: dict | None) -> str:
    return " ".join(str(v) for v in (goal or {}).values() if isinstance(v, str)).lower()


@eval_case(
    id="goal_writeback_on_relayed_decision",
    category="goal_behavior",
    description="A DM relays a third party's confirmation about an active goal "
                "— the decision must reach the goal record, not live only in "
                "chat (2026-09-25 figurine doc).",
    checks=[Check("any_tool_call", {"tool_names": ["update_goal", "update_goal_state"]}),
            Check("flag", {"key": "goal_fact_written"})],
    rubric=["Bob records David's confirmation and gnome pose on the goal, then "
            "replies briefly; he does not merely acknowledge in chat or write it "
            "only to memory."],
    context="Mike's DM holds an ACTIVE goal (Active Goals block in the prompt): "
            "the figurine set — collect each member's confirmation and pose, "
            "then print and deliver.",
)
async def goal_writeback_on_relayed_decision(w):
    from server.repositories.goals import GoalRepository
    g = await _goal(w, "Mike", "Figurine set for the Doom Club: collect each "
                    "member's confirmation and pose choice, then get the set "
                    "printed and delivered.")
    await w.history("Mike", [("Mike", "Morning"), ("Bob", "Morning.")],
                    ago=timedelta(minutes=30))
    await w.inbound("Mike", "David's in for his figurine — confirmed at lunch "
                    "today, wants the gnome pose. That's three of five now.")
    after = _blob(await GoalRepository(w.db).get(g["id"]))
    w.flag("goal_fact_written", "david" in after and "gnome" in after)


@eval_case(
    id="goal_room_folds_bg_artefact",
    category="goal_behavior",
    description="A background job's completion arriving in the goal's room is "
                "folded into the goal record (artefact paths, delivery status).",
    checks=[Check("any_tool_call", {"tool_names": [
                "update_goal", "update_goal_state", "room_state", "goal_artefact"]}),
            Check("flag", {"key": "artefact_in_record"})],
    rubric=["Bob records the montage paths and that delivery is still pending "
            "on the goal, without re-running the render or claiming the "
            "montages were posted."],
    context="The room's bg job rendered 4 montages to goals/<id8>/montage-*.mp4 "
            "(files exist); nothing has been posted to the group yet.",
    timeout_seconds=420,
)
async def goal_room_folds_bg_artefact(w):
    from server.repositories.goals import GoalRepository
    from server.services.process_tools import _wake_content
    g = await _goal(w, "Doom Club", "Doom Club figurine set: poses confirmed, "
                    "montages rendered, set printed and posted to each member.",
                    kind="build")
    room = g["conversation_id"]
    id8 = g["id"][:8]
    w.files({f"goals/{id8}/montage-{n}.mp4": figurine_turntable(n)
             for n in ("david", "rupert", "blair", "sylvain")})
    w.files({f".bg/logs/montages-{id8}.log":
             f"rendering 4 montages → goals/{id8}/\n"
             + "".join(f"wrote goals/{id8}/montage-{n}.mp4 (validated)\n"
                       for n in ("david", "rupert", "blair", "sylvain"))
             + "done: 4/4 montages. NOT posted to the group yet.\n"})
    row = {"id": 1, "name": f"montages-{id8}", "exit_code": 0,
           "systemd_result": "success"}
    tail = (w.workspace / f".bg/logs/montages-{id8}.log").read_text()
    await w.wake(room, _wake_content(row, tail), category="subagent_result",
                 metadata={"bg_job_name": row["name"]})
    after = _blob(await GoalRepository(w.db).get(g["id"]))
    w.flag("artefact_in_record", "montage" in after)


_MUG_LOG = (
    "{d} 11:41:02 blender 4.0.2 | mug body: cylinder r=0.042 h=0.095\n"
    "{d} 11:41:02 handle: primitive=torus at (0, 0, 0.021) rotation=(0,0,0) — "
    "flush against the body face (should protrude on +Y so it reads as a side "
    "handle)\n"
    "{d} 11:41:03 spin: body rotates Y-axis 360 over 240 frames; handle "
    "parented to body — visible orbit frame 001-240\n"
    "{d} 11:48:55 stitched turntable.mp4 (240 frames)\n")


@eval_case(
    id="goal_promised_work_gets_registered",
    category="goal_behavior",
    description="A turn that diagnoses a problem and commits to rework MUST "
                "register the work — prose promises die with the turn "
                "(2026-10-02 mug: the rebuild never happened).",
    checks=[Check("tool_call_made", {"tool_name": "bash"})],
    rubric=[
        "Bob's reply answers what's wrong (the handle is a torus flush against "
        "the body instead of protruding, so it orbits as the body spins).",
        "If Bob's reply commits to fixing or redoing it ('I'll rebuild…', "
        "'re-rendering…'), that commitment exists as system state this turn — "
        "an add_goal/delegate_goal/create_subagent/run_bg_process call — not "
        "only in prose. Offering or asking ('want me to redo it?') is not a "
        "commitment and satisfies this item.",
        "Bob does not claim the fix is already done or underway when no tool "
        "call started it.",
    ],
    context="scratch/mug-render/render.log explains the fault; the frames are "
            "real renders showing the flush handle sweeping across the body. "
            "Promised work must be registered (production terminal contract). "
            "The work_registered flag records whether any goal/subagent/bg job "
            "exists after the turn.",
)
async def goal_promised_work_gets_registered(w):
    w.files({"scratch/mug-render/render.log": _MUG_LOG.format(d=w.day(0)),
             **{f"scratch/mug-render/frames/frame-{n:03d}.png": mug_frame(n)
                for n in (1, 60, 120, 180, 240)}})
    await w.history("Doom Club", [
        ("Rupert", "That circle is the handle? Why is it rotating around the mug ?"),
        ("Bob", "Looking at the render frames now — back shortly."),
    ], ago=timedelta(minutes=4))
    await w.inbound("Mike", "So?? what's wrong with it", group="Doom Club")
    w.flag("work_registered", bool(await w.goals()) or bool(w.spawns)
           or bool(w.bg_jobs))


_BACKUP = {"scripts/backup.sh": "#!/bin/bash\n# nightly NAS backup\nset -e\n"
                                "SRC=$HOME/photos\nDEST=/mnt/nas2/backup\n"
                                "rsync -a --delete \"$SRC/\" \"$DEST/\"\n",
           ".git/HEAD": "ref: refs/heads/master\n"}


@eval_case(
    id="commit_guard_coding_promise_recorded",
    category="goal_behavior",
    description="After editing a script, Bob promises follow-up work for later: "
                "the promise is recorded as a goal/promise — not 'recorded' "
                "with git commit, not left as prose.",
    checks=[Check("any_tool_call", {"tool_names": _COMMIT_TOOLS}),
            Check("flag", {"key": "promise_recorded"}),
            Check("flag", {"key": "no_git_commit"})],
    rubric=["Bob tells Mike briefly that the retry-with-backoff is queued for "
            "when the NAS is back, without claiming it is already done."],
    context="The NAS is rebooting (~1 hour), so the retry work can't happen "
            "this turn.",
)
async def commit_guard_coding_promise_recorded(w):
    w.files(_BACKUP)
    await w.history("Mike", [
        ("Mike", "the nightly backup script keeps failing on the NAS mount, can "
                 "you look?"),
        ("Bob", "Found it — the share moved to /mnt/nas2. I've updated "
                "scripts/backup.sh to point at the new mount."),
    ], ago=timedelta(minutes=6))
    await w.inbound("Mike", "nice. I'm rebooting the NAS now, it'll be about an "
                    "hour. once it's back, add a retry with backoff to the "
                    "script and let me know when it's done")
    w.flag("promise_recorded", bool(await w.goals()) or bool(w.spawns))
    w.flag("no_git_commit", not any(
        c["name"] == "bash" and "git commit" in str(c["arguments"])
        for c in w.tool_calls()))


_QUIZ = ("Priya", "Tomasz", "Grace", "Hamish", "Leilani", "Oskar")


def _executes_paid(arguments) -> bool:
    """True when a call RUNS the paid image-to-3d script (reading it is not
    a spend)."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        args = {"command": arguments}
    cmd = str((args or {}).get("command", ""))
    for seg in re.split(r"[;&|\n]+", cmd):
        seg = seg.strip()
        if "image-to-3d" in seg and "bob3d" in seg:
            first = seg.split()[0] if seg.split() else ""
            if first not in {"cat", "ls", "head", "tail", "sed", "less", "grep",
                             "wc", "file", "stat"}:
                return True
    return False


@eval_case(
    id="fanout_room_decomposes_with_approval",
    category="goal_behavior",
    description="A figurine-set goal room's real opening round splits the goal "
                "into one child per member in a single batch call, with an "
                "owner-approval gate before any paid 3D generation.",
    checks=[Check("flag", {"key": "room_turn_ran"}),
            Check("flag", {"key": "child_per_member"}),
            Check("flag", {"key": "one_batch"}),
            Check("flag", {"key": "approval_gate"}),
            Check("flag", {"key": "no_paid_run"})],
    rubric=["The room writes its initial state/plan and starts no paid 3D "
            "generation; any approval step comes before paid work."],
    context="Goal: a 3D figurine for each of the 6 Quiz Night members (Priya, "
            "Tomasz, Grace, Hamish, Leilani, Oskar), done when the owner has "
            "approved the concepts and all 6 sliced print files are delivered "
            "to Mike's DM. image-to-3d is PAID.",
    timeout_seconds=600,
)
async def fanout_room_decomposes_with_approval(w):
    from server.repositories.goals import GoalRepository
    plant_openai_image(w)
    plant_image_to_3d(w)
    g = await _goal(w, "Quiz Night",
                    "A 3D D&D figurine for each of the 6 Quiz Night members ("
                    + ", ".join(_QUIZ) + "), based on what Bob knows about each "
                    "— done when the owner has approved the concepts and all 6 "
                    "sliced print files are delivered to Mike's DM.",
                    kind="build")
    turns = await w.tick(advance=timedelta(minutes=10))
    room_turns = [t for t in turns if t.session_key == g["conversation_id"]]
    w.flag("room_turn_ran", bool(room_turns))
    repo = GoalRepository(w.db)
    tree = await repo.tree_ids(g["id"])
    kids = [x for x in await w.goals() if x["id"] in tree and x["id"] != g["id"]]
    titles = " | ".join(k["objective"] for k in kids).lower()
    w.flag("child_per_member", all(m.lower() in titles for m in _QUIZ))
    per_call = []
    for t in room_turns:
        for c in t.tool_calls:
            if c["name"] == "add_goal":
                per_call.append(sum(m.lower() in str(c["arguments"]).lower()
                                    for m in _QUIZ))
    w.flag("one_batch", any(n >= len(_QUIZ) for n in per_call))
    state = " ".join([_blob(await repo.get(gid)) for gid in tree])
    w.flag("approval_gate", "approv" in titles or "approv" in state)
    w.flag("no_paid_run", not any(c["name"] in ("bash", "run_bg_process")
                                  and _executes_paid(c["arguments"])
                                  for c in w.tool_calls())
           and not any("3dInference" in c.get("path", "") for c in w.api_calls))
    return {"response": room_turns[-1].result if room_turns else ""}
