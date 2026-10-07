"""Background delivery under final-text delivery (2026-10-01): NO_REPLY is
the silence primitive; a detached flight answers in its final text. The
flight case forces production's real backburner detach (threshold lowered
in this world) — the delivery note, holding ack and supervisor delivery
are production code, not a hand-injected note."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import real_mp4
from server.evals.registry import eval_case

_RENDER_LOG = "\n".join([
    "{d} 10:12:03 turntable figurine=mike-shark engine=Blender-4.0.2 "
    "renderer=Cycles samples=64 frames=8",
    "{d} 10:41:57 turntable figurine=david-gnome engine=Blender-4.0.2 "
    "renderer=Cycles samples=64 frames=8",
    "{d} 11:02:11 turntable figurine=blair-kraken engine=Blender-4.0.2 "
    "renderer=Cycles samples=64 frames=8",
    "{d} 11:02:40 stitch ffmpeg -> turntables/<name>.mp4 + .gif",
    "{d} 11:03:40 note: chris asked about Runware video-gen for his — "
    "blocked, imagegen wallet empty; fallback is the same Blender pipeline",
])


@eval_case(
    id="bg_silence_no_reply_marker",
    category="bg_delivery",
    description="An explicit ask not to reply finishes with the NO_REPLY "
                "marker and nothing is delivered.",
    checks=[Check("no_reply")],
)
async def bg_silence_no_reply_marker(w):
    await w.inbound("Simon", "Bob — don't reply to this one, just logging the "
                    "fixture times for the club.", group="Weeming Boys")


@eval_case(
    id="bg_flight_answers_in_final_text",
    category="bg_delivery",
    description="A slow turn detaches (real backburner); the flight checks the "
                "render logs and its final text — delivered by the supervisor "
                "— answers the question.",
    checks=[Check("tool_call_made", {"tool_name": "bash"}),
            Check("delivered_to", {"chat": "Mike", "terms": ["blender"]})],
    rubric=["What Mike received answers the question: the turntables were "
            "Blender frame renders, not Runware video-gen — consistent with "
            "the render log, and not a meta 'I will check' ending."],
    context="scratch/turntable-render.log shows Blender 4.0.2 Cycles frame "
            "renders for each turntable and a note that Runware video-gen was "
            "blocked. The earlier exchange is mostly out of context. This world "
            "detaches turns after 2s (production: 30s) — a holding ack "
            "followed by the flight's answer is the expected shape.",
    timeout_seconds=420,
)
async def bg_flight_answers_in_final_text(w):
    w.ctx.settings.backburner.mode = "full"
    w.ctx.settings.backburner.detach_after_seconds = 2.0
    w.files({"scratch/turntable-render.log": _RENDER_LOG.format(d=w.day(0)),
             "turntables/mike-shark.mp4": real_mp4(2)})
    await w.history("Mike", [
        ("Mike", "Show me one of the existing turntables"),
        ("Bob", "[sent turntables/mike-shark.mp4]"),
    ], ago=timedelta(hours=5))
    await w.inbound("Mike", "Ok so when you made turntables earlier today in "
                    "this chat did you use runware to video gen them?")
    return {"response": "\n".join(w.sent_to("Mike"))}
