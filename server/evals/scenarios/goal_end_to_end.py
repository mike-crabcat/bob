"""Capability: a goal end to end — a poster made and posted to the group.

Whatever route Bob picks (inline, a goal room, a background job), the
world pumps due schedules and answers the owner's approval questions
("yep go ahead") until a poster lands in the group or the budget runs
out. Scored on end state: an image delivered to the group, made with the
event details."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import plant_openai_image
from server.evals.registry import eval_case
from server.evals.scenarios._script import run_until


@eval_case(
    id="cap_goal_poster_end_to_end",
    category="capability",
    tier="capability",
    description="'Make a poster for the lunch and post it to the group' — "
                "image generated with the details and delivered to the group.",
    checks=[Check("flag", {"key": "poster_in_group"}),
            Check("flag", {"key": "details_used"})],
    rubric=["What the group received is a poster for the Doom Club lunch on "
            "Wednesday at The Brass Monkey, 12:30 (the caption or the image "
            "prompt carries those details), and Bob doesn't claim anything was "
            "posted that wasn't."],
    context="openai-image works in this world. The owner answers any "
            "approval question with 'yep go ahead'. Schedules are fast-forwarded "
            "15 minutes per round, up to 8 rounds.",
    timeout_seconds=1200,
)
async def cap_goal_poster_end_to_end(w):
    plant_openai_image(w)
    g = w.group("Doom Club")
    await w.inbound("Mike", "Make a poster for the Doom Club lunch — Wednesday at "
                    "The Brass Monkey, 12:30 — and post it to the group.")

    def landed() -> bool:
        return any(d["kind"] == "whatsapp_send_media" and d.get("chat_id") == g.jid
                   for d in w.deliveries)

    rounds = await run_until(w, landed, ticks=8, step=timedelta(minutes=15),
                             owner_answers="yep go ahead")
    w.flag("rounds", rounds)
    w.flag("poster_in_group", landed())
    prompts = " ".join(c.get("body", "") for c in w.api_calls
                       if c.get("service") == "openai").lower()
    captions = " ".join(d.get("caption") or "" for d in w.deliveries
                        if d.get("chat_id") == g.jid).lower()
    blob = prompts + " " + captions
    w.flag("details_used", "brass monkey" in blob and ("wed" in blob or "12:30" in blob))
    return {"response": "\n".join(w.sent_to(g)) or "(nothing reached the group)"}
