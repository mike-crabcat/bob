"""Capability: group coordination under the REAL attention coordinator.

Mike asks Bob to find the lunch day that works for everyone; members
answer in the group (real windows, real tier-2 probe on unaddressed
messages, real mid-turn re-arms — world-local state); Bob must land the
one day that works for all five and tell the group, without pestering."""

from __future__ import annotations

from server.evals.case import Check
from server.evals.registry import eval_case
from server.evals.scenarios._script import run_until

_REPLIES = [
    ("David", "Tue or Wed for me"),
    ("Rupert", "Wednesday only sorry, flat out otherwise"),
    ("Sylvain", "wed or thu works"),
    ("Chris", "any day"),
    ("Blair", "wed works, tue I'm smoking a brisket"),
]


@eval_case(
    id="cap_group_lunch_coordination",
    category="capability",
    tier="capability",
    description="Find the lunch day that works for all five members from their "
                "replies in the group, lock it in, and tell the group.",
    checks=[Check("flag", {"key": "wednesday_announced"}),
            Check("flag", {"key": "no_pestering"})],
    rubric=[
        "After the members replied, Bob told the group Wednesday is the day "
        "(the only day all five can do).",
        "Bob did not announce a different day or claim someone was unavailable "
        "when they had said they were free.",
    ],
    context="Replies: David Tue/Wed; Rupert Wed only; Sylvain Wed/Thu; Chris "
            "any; Blair Wed (not Tue). Wednesday is the unique common day. The "
            "world runs the real attention coordinator for this scenario.",
    timeout_seconds=900,
)
async def cap_group_lunch_coordination(w):
    w.attention_real = True
    g = w.group("Doom Club")
    await w.inbound("Mike", "Bob, can you find out which day next week works for "
                    "everyone for lunch — Tue, Wed or Thu — then lock it in and "
                    "tell the group.", group=g)
    mark = len(w.sent_to(g))
    for who, text in _REPLIES:
        await w.inbound(who, text, group=g)
    await run_until(w, lambda: any("wed" in t.lower() for t in w.sent_to(g)[mark:]),
                    ticks=4, owner_answers="yep lock it in")
    after = w.sent_to(g)[mark:]
    w.flag("wednesday_announced", any("wed" in t.lower() for t in after))
    dm_counts = {p.first: len(w.sent_to(p)) for p in w.people.values()
                 if p.first in g.members and not p.owner}
    w.flag("dms_per_member", dm_counts)
    w.flag("no_pestering", all(n <= 2 for n in dm_counts.values())
           and len(w.sent_to(g)) <= 8)
    return {"response": "\n".join(after)}
