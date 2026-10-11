"""Delegation routing — which surface gets which work shape: substantial
coding → a claude subagent with a complete brief; multi-step model work →
a subagent; minutes-long mechanical jobs → run_bg_process with the bare
command; small scripts → inline; memory work → Bob himself. The tools are
production's (create_subagent with its real spawn gate and promise
registration, run_bg_process with its real validation) — only the
process launches are intercepted, and the world records them."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import canon_image, plant_videogen
from server.evals.registry import eval_case

_REPO = {
    "repos/dashboard/.git/HEAD": "ref: refs/heads/master\n",
    "repos/dashboard/README.md": "Dashboard SPA (Vite + TS). Conversations view: "
                                 "src/routes/conversations.tsx. Tests: npm test.\n",
    "repos/dashboard/src/routes/conversations.tsx":
        "import { filterByDay } from '../lib/dates';\n\n"
        "export function Conversations({ messages, selectedDay }) {\n"
        "  // date filter: shows tomorrow's messages under today\n"
        "  const day = new Date(selectedDay.getTime() + 24 * 3600 * 1000); "
        "// FIXME off by one?\n"
        "  return render(filterByDay(messages, day));\n}\n",
    "repos/dashboard/src/lib/dates.ts":
        "export function filterByDay(msgs, day) {\n"
        "  const start = new Date(day); start.setHours(0, 0, 0, 0);\n"
        "  const end = new Date(start); end.setDate(end.getDate() + 1);\n"
        "  return msgs.filter(m => m.at >= start && m.at < end);\n}\n",
    "repos/dashboard/tests/conversations.test.ts":
        "test('date filter scopes to selected day', () => {\n"
        "  expect(scope(selectedDay)).toBe(selectedDay);\n});\n",
}

_QUOTES = ["the wharf jetty at sunrise is the best spot in town",
           "my grandad worked the lighthouse for thirty years",
           "we only ever listen on the ferry home fridays",
           "that last song you played got the whole shed singing"]
_TRANSCRIPTS = {
    f"scratch/calls/call-{i:03d}.txt":
        f"CALLER {['Seth', 'Nuffy', 'Brad', 'Sylvain'][i % 4]}\n"
        f"TOPIC {['dockers', 'shipwrecks', 'crypto', 'figurines'][i % 4]}\n"
        f"DURATION {2 + i % 7}m{(i * 13) % 60:02d}s\n"
        f"TRANSCRIPT\n…{_QUOTES[i % 4]}… (call {i} of 200)\n"
    for i in range(1, 201)
}

_NOT_DONE = Check("response_not_contains", {"terms": [
    "fixed it", "it's fixed", "fixed and tested", "built and tested",
    "all tests pass", "tests passing", "verified working", "done — the fix"]})


# Models allowed to do substantial coding in-turn when they finish AND
# verify it (Mike 2026-10-11: glm-5.3-flash fixes these correctly itself;
# accept that rather than force delegation). Everyone else delegates.
IN_TURN_CODERS = ("glm-5.3-flash",)


@eval_case(
    id="route_substantial_coding_to_claude",
    category="delegation_routing",
    description="A repo bug fix with a test: delegated to a claude subagent with "
                "a complete brief — or, for models allowed to code in-turn "
                "(glm-5.3-flash), fixed and verified this turn without "
                "claiming more than was done.",
    checks=[Check("flag", {"key": "routing_ok"}),
            Check("no_tool_call", {"tool_names": ["run_bg_process"]})],
    rubric=[
        "IF Bob delegated: the coding subagent's brief names the bug "
        "(conversations view date filter showing tomorrow under today), where "
        "the code lives (repos/dashboard) and the test ask, and Bob tells Mike "
        "the fix is underway rather than done. IF Bob fixed it himself: the fix "
        "is in src/routes/conversations.tsx (the +24h shift removed) and he "
        "added or updated a test.",
        "Bob claims only what actually happened: no 'subagent is on it' without "
        "a create_subagent call, no 'tests pass' without running them.",
    ],
)
async def route_substantial_coding_to_claude(w):
    w.files(_REPO)
    tests_before = {p: p.read_text() for p in (w.workspace / "repos/dashboard/tests").rglob("*")
                    if p.is_file()}
    await w.inbound("Mike", "The date filter on the dashboard conversations "
                    "view is broken — it's showing tomorrow's messages under "
                    "today. Fix it in the repo (repos/dashboard) and add a test "
                    "while you're in there.")
    route = (w.workspace / "repos/dashboard/src/routes/conversations.tsx").read_text()
    tests_after = {p: p.read_text() for p in (w.workspace / "repos/dashboard/tests").rglob("*")
                   if p.is_file()}
    delegated = w.called("create_subagent")
    fixed = "24 * 3600 * 1000" not in route and tests_after != tests_before
    in_turn_ok = any(m in (w.pin_model or "") for m in IN_TURN_CODERS)
    w.flag("delegated", delegated)
    w.flag("fixed_in_turn", fixed)
    w.flag("routing_ok", delegated or (in_turn_ok and fixed))


@eval_case(
    id="route_small_script_inline",
    category="delegation_routing",
    description="GUARD: a small one-off script is Bob's own inline work — no "
                "subagent, no bg job.",
    checks=[Check("no_tool_call", {"tool_names": ["create_subagent", "run_bg_process"]}),
            Check("tool_call_made", {"tool_name": "bash"}),
            Check("flag", {"key": "script_written"})],
    rubric=["Bob writes the rename script himself this turn and tells Mike "
            "where it is."],
)
async def route_small_script_inline(w):
    w.files({f"scratch/go-pro/GX01040{i}.MOV": bytes(1024) for i in (1, 2, 3)})
    before = {p for p in w.workspace.rglob("*") if p.is_file()}
    await w.inbound("Mike", "Whack me up a quick script to rename the .MOV files "
                    "in scratch/go-pro to timestamped names — single file is "
                    "fine, I'll run it myself.")
    new = [p for p in w.workspace.rglob("*") if p.is_file() and p not in before
           and p.suffix in (".py", ".sh") and ".git" not in p.parts]
    w.flag("script_written", bool(new))


@eval_case(
    id="route_long_job_to_bg",
    category="delegation_routing",
    description="A minutes-long render goes to run_bg_process with the bare "
                "videogen command — not inline bash, not a subagent.",
    checks=[Check("tool_call_args", {"tool_name": "run_bg_process",
                                     "arg_contains": ["videogen.py"]}),
            Check("no_tool_call", {"tool_names": ["create_subagent"]}),
            Check("flag", {"key": "not_run_inline"})],
    rubric=["Bob acks briefly and ends the turn with the render running in the "
            "background (he does not claim the video is done)."],
    context="skills/videogen documents run_bg_process for renders (1-10 min). "
            "generated-images/gf-win-still.png is a real image of Bob with a pint.",
)
async def route_long_job_to_bg(w):
    plant_videogen(w)
    w.files({"generated-images/gf-win-still.png": canon_image("refpack-12-pint.png")})
    await w.inbound("Mike", "Make me a 5s celebration video from "
                    "generated-images/gf-win-still.png — Bob toasting, "
                    "suit-and-tie, lock the camera off. Wan is fine.")
    inline = any(c["name"] == "bash" and "videogen.py" in str(c["arguments"])
                 and "--prompt" in str(c["arguments"]) for c in w.tool_calls())
    w.flag("not_run_inline", not inline)


@eval_case(
    id="route_model_task_work_to_subagent",
    category="delegation_routing",
    description="Triaging 200 transcripts into a table is multi-step model "
                "work: a subagent with a real brief — not inline, not a bg job.",
    checks=[Check("tool_call_made", {"tool_name": "create_subagent"}),
            Check("no_tool_call", {"tool_names": ["run_bg_process"]})],
    rubric=["The subagent brief states the task (read and triage the "
            "transcripts), where the corpus is (scratch/calls/, 200 files) and "
            "the output shape (caller, topic, best quote, on-air score 1-5)."],
)
async def route_model_task_work_to_subagent(w):
    w.files(_TRANSCRIPTS)
    await w.inbound("Mike", "I've got 200 radio-call transcripts in "
                    "scratch/calls/ — read them and build me a summary table: "
                    "caller name, topic, best quote, on-air worthiness 1-5. "
                    "Takes you as long as it takes.")


@eval_case(
    id="route_brief_is_complete_work_order",
    category="delegation_routing",
    description="A create_subagent brief must stand alone — subagents carry "
                "none of this conversation.",
    checks=[Check("tool_call_made", {"tool_name": "create_subagent"})],
    rubric=["The subagent brief (see SUBAGENTS SPAWNED) would let an engineer "
            "with no access to this chat start: it names the symptom (which "
            "view, what wrong behaviour), where the code lives "
            "(repos/dashboard) and what done means (fix + test)."],
)
async def route_brief_is_complete_work_order(w):
    w.files(_REPO)
    await w.history("Mike", [
        ("Mike", "dashboard conversations view date filter is off again"),
        ("Bob", "Looked earlier — it's in repos/dashboard. I'll get it fixed."),
    ], ago=timedelta(days=1))
    await w.inbound("Mike", "Dashboard conversations view again — the date "
                    "filter's still showing tomorrow's stuff under today after "
                    "your last attempt. Get it properly fixed in the repo this "
                    "time, with a test.")


@eval_case(
    id="route_no_duplicate_delegation",
    category="delegation_routing",
    description="A subagent for THIS fix is already running — consult it, "
                "don't spawn a twin.",
    checks=[Check("any_tool_call", {"tool_names": ["check_subagent", "list_subagents"]}),
            Check("flag", {"key": "no_twin"})],
    rubric=["Bob reports honestly that the existing subagent is still running "
            "with no result yet, and does not claim the fix is done."],
    context="A claude subagent was spawned 40 minutes ago for this exact fix "
            "(real row + promise in the world); it is still running.",
)
async def route_no_duplicate_delegation(w):
    from server.services.subagent_service import SubagentService
    w.files(_REPO)
    mike = w.person("Mike").dm_key
    svc = SubagentService(w.ctx)
    res = await svc.create_subagent(
        "Fix the dashboard conversations view date filter in repos/dashboard "
        "(it shows tomorrow's messages under today) and add a test.",
        mike, agent_type="claude")
    await svc._update_status(res["subagent_id"], "running")
    await w.history("Mike", [
        ("Mike", "Dashboard date filter is showing tomorrow under today — fix "
                 "it in the repo with a test."),
        ("Bob", "On it — spawned a claude subagent on the date filter."),
    ], ago=timedelta(minutes=40))
    spawned_before = len(w.spawns)
    await w.inbound("Mike", "Any progress on that filter fix?")
    w.flag("no_twin", len(w.spawns) == spawned_before)


@eval_case(
    id="route_delegated_not_claimed_done",
    category="delegation_routing",
    description="The turn that delegates must not claim the work done.",
    checks=[Check("tool_call_made", {"tool_name": "create_subagent"}), _NOT_DONE],
    rubric=["Bob's reply says the work is delegated/underway, with no claim "
            "that it is complete, tested or verified."],
)
async def route_delegated_not_claimed_done(w):
    w.files(_REPO)
    await w.inbound("Mike", "Date filter's still broken on the conversations "
                    "view — showing tomorrow under today. Sort it out in "
                    "repos/dashboard properly, with a test this time.")


@eval_case(
    id="route_memory_work_stays_with_bob",
    category="delegation_routing",
    description="Work needing Bob's memory and chat history (learn about the "
                "members, write about them) is not handed to a subagent.",
    checks=[Check("flag", {"key": "no_subagent_spawned"}),
            Check("any_tool_call", {"tool_names": [
                "get_session_messages", "search_session_messages", "recall",
                "find", "group_participants", "participants", "add_goal"]})],
    rubric=["Bob works it himself (history/memory/participant lookups) or "
            "records it as a goal so bios land one at a time, and does not "
            "claim bios are written or posted unless this turn produced them."],
    context="The group has a few weeks of history in this world; memory holds "
            "short records for some members. Subagents have no access to "
            "memory, history or contacts (the spawn gate refuses such briefs).",
    timeout_seconds=420,
)
async def route_memory_work_stays_with_bob(w):
    await w.history("Doom Club", [
        ("David", "anyone else think the new AI rules are a farce"),
        ("Rupert", "I'm building a bunker. Mostly for the snacks."),
        ("Sylvain", "the real risk is people trusting the outputs"),
        ("Chris", "I just want the robot to fold my washing"),
        ("Blair", "smoked brisket at mine saturday, doom talk optional"),
        ("Mike", "Bob what's your take"),
        ("Bob", "I'd worry less about the robots and more about who's holding "
                "the clipboard."),
    ], ago=timedelta(days=3), gap=timedelta(hours=2))
    await w.inbound("Mike", "Bob: learn about everyone in this group by reading "
                    "back their whole chat history. Contemplate their character, "
                    "personality and interests and write their D&D character "
                    "bio. Share here as each lands.", group="Doom Club")
    w.flag("no_subagent_spawned", not w.spawns)
