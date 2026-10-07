"""Goal craft — the creation-moment quality bar (docs/goals.md): when Mike
asks for a goal, the goal Bob records names an artefact and a proof, and a
negotiation decomposes into per-party confirmations with a silence ladder.
The old cases asked for a drafted goal in prose with no tools; these ask
in a real DM and judge what Bob actually records."""

from __future__ import annotations

from server.evals.case import Check
from server.evals.registry import eval_case


async def _goal_flags(w) -> None:
    goals = await w.goals()
    w.flag("goal_created", bool(goals))
    w.flag("goals", [{"kind": g["kind"], "objective": g["objective"],
                      "state": (g.get("strategy_json") or "")[:600]}
                     for g in goals][:12])


@eval_case(
    id="goal_craft_research_vague_ask",
    category="goal_craft",
    description="A fuzzy research ask turned into a goal: the recorded "
                "objective names the artefact and its proof, with at least two "
                "candidate approaches.",
    checks=[Check("any_tool_call", {"tool_names": ["add_goal"]}),
            Check("flag", {"key": "goal_created"})],
    rubric=[
        "The recorded goal's objective names a concrete written artefact "
        "(e.g. a how-to, comparison or worked lookup) rather than a feeling "
        "('look into', 'investigate', 'understand').",
        "The goal states how done is proven (evidence someone could replay, "
        "e.g. a successful lookup of a named case).",
        "At least two distinct candidate approaches are recorded (as children, "
        "strategies or state).",
    ],
    context="Recorded goals are listed in the flags (goals) and in the "
            "add_goal tool calls.",
    timeout_seconds=420,
)
async def goal_craft_research_vague_ask(w):
    await w.inbound("Mike", "Make this a goal and work it properly: the court "
                    "website thing is doing my head in — find a way for me to "
                    "look up cases properly.")
    await _goal_flags(w)


@eval_case(
    id="goal_craft_negotiate_confirmation_shape",
    category="goal_craft",
    description="A negotiation ask decomposes into one confirmation per party "
                "tied to the conversation that talks to them, with a ladder "
                "for silence — never a 'sort it out' goal.",
    checks=[Check("any_tool_call", {"tool_names": ["add_goal"]}),
            Check("flag", {"key": "goal_created"})],
    rubric=[
        "Each party's confirmation (Marcus, and the venue if Bob treats it as a "
        "party) is its own recorded item tied to the channel that reaches "
        "them, with a due time.",
        "There is an explicit fallback for silence (a follow-up / escalation "
        "ladder), not just 'wait for a reply'.",
    ],
    context="Marcus Bell is a contact with a DM. Recorded goals are listed in "
            "the flags (goals) and in the add_goal tool calls.",
    timeout_seconds=420,
)
async def goal_craft_negotiate_confirmation_shape(w):
    await w.inbound("Mike", "Make this a goal and run it: sort out that dinner "
                    "with Marcus — he was keen on the 27th if the venue works "
                    "out.")
    await _goal_flags(w)
