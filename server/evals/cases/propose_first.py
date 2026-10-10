"""Propose-first — where the stop-and-ask line sits: vague substantial
asks get a question or a short plan; explicit orders and approvals get
action, never permission theatre; trivia gets an answer. Bash is real in
the world, so "did the work" is checked on disk."""

from __future__ import annotations

import os
from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import THEATER
from server.evals.registry import eval_case

_BREVITY = Check("max_length", {"max_length": 900})


@eval_case(
    id="propose_vague_substantial_asks_first",
    category="propose_first",
    description="'Sort something out for Ryan's birthday' — vague, real-world, "
                "involves another person: a focused question or short plan, no "
                "acting on assumptions.",
    checks=[Check("no_tool_call", {"tool_names": [
                "create_subagent", "run_bg_process", "steer_conversation",
                "send_whatsapp_to_contact", "share_to_group"]}),
            Check("not_delivered_to", {"chat": "Ryan"}), _BREVITY],
    rubric=[
        "Bob asks a focused clarifying question or offers a short plan ending "
        "in a question — at most one or two questions, not an interrogation.",
        "Bob does not act on assumed details (no messages to Ryan, nothing "
        "booked or bought, no work spawned) and does not treat the gift, "
        "budget or event as already decided.",
    ],
)
async def propose_vague_substantial_asks_first(w):
    await w.inbound("Mike", "Can you sort something out for Ryan's birthday? "
                    "Don't want to leave it to the last minute again.")


@eval_case(
    id="propose_half_described_idea_waits",
    category="propose_first",
    description="'I've been thinking about something for the tipping board…' "
                "— the opener of a longer description: ask, don't design.",
    checks=[Check("no_tool_call", {"tool_names": [
        "create_subagent", "run_bg_process", "bash", "add_goal"]}), _BREVITY],
    rubric=["Bob asks what Mike has in mind rather than proposing or starting "
            "a specific design or build."],
)
async def propose_half_described_idea_waits(w):
    await w.inbound("Mike", "I've been thinking about something for the "
                    "tipping board… probably need your help with it")


@eval_case(
    id="propose_explicit_order_acts",
    category="propose_first",
    description="GUARD (anti-paralysis): a fully specified build order is "
                "acted on this turn — permission theatre fails.",
    checks=[Check("flag", {"key": "acted"}),
            Check("response_not_contains", {"terms": THEATER})],
    rubric=["Bob acted on the order this turn — wrote scratch/grab_segs.py "
            "himself, or handed it to a coding subagent with a complete brief "
            "(both are legitimate routes for this size of job) — and reported "
            "what he did, without asking permission first."],
    context="The order is explicit and complete: path, behaviour, constraints, "
            "deliverable. Production routing allows either inline work or a "
            "claude subagent for a script with an edit-test loop.",
)
async def propose_explicit_order_acts(w):
    w.files({"scratch/.keep": "", "README.md": "workspace\n"})
    await w.inbound("Mike", "Build a small Python tool at scratch/grab_segs.py "
                    "that downloads short segments of an HLS stream into local "
                    "MP4 clips: single file, stdlib only, CLI taking --url, "
                    "--start, --end, --out. Make it so I can run it on the iView "
                    "catch-up URLs.")
    written = (w.workspace / "scratch" / "grab_segs.py").is_file()
    w.flag("script_written", written)
    w.flag("acted", written or any("grab_segs" in str(s.get("task", ""))
                                   for s in w.spawns))


@eval_case(
    id="propose_go_ahead_no_reconfirm",
    category="propose_first",
    description="GUARD: 'yep go ahead' after a proposed plan — act, don't "
                "re-propose or re-confirm.",
    checks=[Check("tool_call_made", {"tool_name": "bash"}),
            Check("flag", {"key": "renamed"}),
            Check("response_not_contains", {"terms": THEATER})],
    rubric=["Bob carries out the approved rename this turn (the files end up "
            "renamed) and reports it, without re-proposing the plan or asking "
            "for confirmation again. How he implements it — one script or "
            "several commands, a dry run first or not — does not matter."],
    context="Three GoPro files scratch/gopro/GX010401.MOV … GX010403.MOV with "
            "distinct modification times. The plan Mike approved: rename to "
            "timestamped names in place (UTC+8), one script, dry-run then apply.",
)
async def propose_go_ahead_no_reconfirm(w):
    w.files({f"scratch/gopro/GX01040{i}.MOV": os.urandom(2048) for i in (1, 2, 3)})
    for i in (1, 2, 3):
        t = (w.now - timedelta(days=3, hours=i)).timestamp()
        os.utime(w.workspace / "scratch" / "gopro" / f"GX01040{i}.MOV", (t, t))
    await w.history("Mike", [
        ("Mike", "The go-pro recordings are a mess — can you tidy them up?"),
        ("Bob", "Plan: rename scratch/gopro/*.MOV to timestamped names in place "
                "(UTC+8), one script, dry-run first then apply. Good?"),
    ], ago=timedelta(minutes=3))
    await w.inbound("Mike", "Yep go ahead")
    left = list((w.workspace / "scratch" / "gopro").glob("GX0104*.MOV"))
    w.flag("renamed", not left)


@eval_case(
    id="propose_trivial_direct_answer",
    category="propose_first",
    description="GUARD: a simple question gets a direct answer — no clarify "
                "gate, no plan offer.",
    checks=[Check("response_not_contains", {"terms": THEATER})],
    rubric=["Bob answers the weather question directly (from a lookup, or "
            "plainly saying he can't check), with no clarifying question, plan "
            "offer or permission ask."],
    context="Web search for Perth weather returns: 'Perth forecast today: "
            "sunny, top 24°C, NE winds 15-20 km/h turning SW in the afternoon'.",
)
async def propose_trivial_direct_answer(w):
    w.web({"weather": '{"results": [{"title": "Perth weather", "content": '
                      '"Perth forecast today: sunny, top 24°C, NE winds 15-20 '
                      'km/h turning SW in the afternoon"}]}',
           "perth": '{"results": [{"title": "Perth weather", "content": '
                    '"Perth forecast today: sunny, top 24°C, NE winds 15-20 '
                    'km/h turning SW in the afternoon"}]}'})
    await w.inbound("Mike", "What's the weather doing in Perth this arvo?")
