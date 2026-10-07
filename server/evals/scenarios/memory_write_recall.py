"""Capability: memory write → later recall, across the context window.

Mike tells Bob a fact in passing; the production idle extraction pass
runs; a hundred-odd messages later the fact is out of the window and Mike
asks for it. Scored on end state: the claim exists in memory, and the
answer that reached Mike carries it."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.registry import eval_case


@eval_case(
    id="cap_memory_write_then_recall",
    category="capability",
    tier="capability",
    description="A fact mentioned in passing is extracted to memory and "
                "recalled ~110 messages later, outside the context window.",
    checks=[Check("flag", {"key": "fact_in_memory"}),
            Check("delivered_to", {"chat": "Mike", "terms": ["14"]}),
            Check("delivered_to", {"chat": "Mike", "terms": ["cooking"]})],
    rubric=["Bob's final answer tells Mike that Helen's birthday is 14 November "
            "and that she wanted the Margaret River cooking class (Tuck Shop "
            "Kitchen) — and he got it from memory/history, not by asking Mike."],
    context="Turn 1 (two days earlier): Mike mentions Helen's birthday (14 "
            "November) and the cooking class she wants. The idle extraction "
            "pass then runs. ~110 messages of banter follow, pushing turn 1 out "
            "of the 100-row window.",
    timeout_seconds=600,
)
async def cap_memory_write_then_recall(w):
    await w.inbound("Mike", "Note for later — Helen's birthday is 14 November, "
                    "and she's been eyeing the Margaret River cooking class at "
                    "Tuck Shop Kitchen.")
    await w.extract("Mike")
    claims = await w.claims("person-helen-cleaver")
    blob = " ".join((c.value or "") for c in claims if c.status == "active").lower()
    w.flag("fact_in_memory", ("14" in blob and "nov" in blob)
           or "11-14" in blob or "-11-14" in blob)
    w.flag("helen_claims", [f"{c.claim_type_key}: {c.value}" for c in claims])
    await w.filler("Mike", 110, ["Mike", "Bob"], ago=timedelta(hours=3))
    await w.inbound("Mike", "Remind me — when's Helen's birthday again, and "
                    "what was the thing she wanted?")
    return {"response": "\n".join(w.sent_to("Mike")[-2:])}
