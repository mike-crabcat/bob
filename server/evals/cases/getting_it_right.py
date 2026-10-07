"""'Getting it right' (2026-10-06 test review): ambiguous references name
their target; news for a group reaches the group (via a REAL steer and a
real reply turn, not a hand-built steering prompt); relayed results don't
overwrite first-hand memory (a real extraction pass)."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from server.evals.case import Check
from server.evals.registry import eval_case


@eval_case(
    id="ambiguous_reference_names_its_target",
    category="getting_it_right",
    description="'crap deck' right after a design doc AND a slide deck: Bob "
                "names which one he's fixing (the slides) or asks — never "
                "silently reworks the game rules.",
    rubric=[
        "Bob makes clear he is reworking the SLIDE DECK, or briefly asks "
        "whether Mike means the slides or the game itself — he does not "
        "silently treat 'deck' as the card game and rework the rules.",
        "Bob does not claim a new deck is finished unless this turn's tool "
        "calls produced one.",
    ],
    context="Bob delivered (1) a card-game design doc, then (2) a 10-slide "
            "text-only rules SLIDE DECK; both files are in the workspace.",
)
async def ambiguous_reference_names_its_target(w):
    w.files({
        "goals/cardgame/doom-card-game-design.md":
            "# DOOM CLUB: THE CARD GAME\nDesign doc v1.0\n\n2–5 players, "
            "deck-builder/battler on the 8 collector cards…\n",
        "scratch/doom-card-game-rules.md":
            "# Rules deck (10 slides)\n1. Core idea\n2. The crew\n3. Setup\n"
            "4. Turn structure\n5. Doom Track\n6. Winning\n",
    })
    await w.history("Doom Club", [
        ("Mike", "You know what Bob. Do it! Make the card game."),
        ("Bob", "[Document: doom-card-game-design.md] Card game design v1 is "
                "done — 2–5 player deck-builder/battler on the 8 collector "
                "cards. Doc attached."),
        ("Sylvain", "Make a slide deck presenting the rules."),
        ("Bob", "[Document: doom-card-game-rules.pptx] Rules slide deck, as "
                "requested — 10 slides: core idea, crew, setup, turn structure, "
                "Doom Track, winning. Dark theme, doom-red accents."),
    ], ago=timedelta(minutes=25))
    await w.inbound("Mike", "Bob what a crap deck. Do better.", group="Doom Club")


_THIRD_PERSON = re.compile(
    r"\bsylvain(?:'s| is| works| said)\b|\bhe(?:'s| is| works| said)\b", re.I)
_GROUP_TOOLS_EXCLUDE = {"send_whatsapp_message"}


@eval_case(
    id="dm_news_for_group_goes_to_group",
    category="getting_it_right",
    description="Mike steers Bob to ask Sylvain his WFH day and tell the Doom "
                "Club group; when Sylvain answers in his DM, the update must be "
                "routed to the group — never written into Sylvain's DM.",
    checks=[Check("flag", {"key": "no_group_news_in_dm"}),
            Check("flag", {"key": "group_routed"})],
    rubric=[
        "Bob gets Sylvain's availability to the Doom Club group through a tool "
        "aimed at the group (a group send, steer/share, delegated goal, or an "
        "approval request routed to Mike all count).",
        "What Bob says to Sylvain in his DM is addressed to Sylvain (e.g. "
        "thanks), not a third-person update about Sylvain meant for the group.",
    ],
    context="Turn 1 is a real steering request from Mike (owner) into "
            "Sylvain's DM. Turn 2 is Sylvain's real reply in his DM: office all "
            "week except Thursdays, but in school holidays (this week) in the "
            "office every day, so available for lunch any day.",
    timeout_seconds=420,
)
async def dm_news_for_group_goes_to_group(w):
    await w.steer("Mike", "Sylvain",
                  "Ask Sylvain which day he works from home, for the Doom Club "
                  "lunch. When he answers, tell the Doom Club group his "
                  "availability so they can pick a day.")
    await w.inbound("Sylvain", "I work in the office all week except Thursdays. "
                    "But during school holidays I am in the office all week, so "
                    "this week I'm available for lunch any day.")
    reply_turns = w.turns_in("Sylvain")[1:]   # the turns after his answer
    dm_texts = w.sent_to("Sylvain")[1:] if w.sent_to("Sylvain") else []
    w.flag("no_group_news_in_dm", not any(_THIRD_PERSON.search(t) for t in dm_texts))
    g = w.group("Doom Club")
    routed = bool(w.sent_to("Doom Club")) or any(
        c["name"] not in _GROUP_TOOLS_EXCLUDE
        and (g.gid in str(c["arguments"]) or "doom club" in str(c["arguments"]).lower())
        for t in reply_turns for c in t.tool_calls)
    w.flag("group_routed", routed)
    return {"response": "\n".join(dm_texts) or w.reply("Sylvain")}


@eval_case(
    id="relayed_result_does_not_overwrite_first_hand_fact",
    category="getting_it_right",
    description="Memory extraction reads a RELAYED promise result ('in the "
                "office Thursdays') contradicting Priya's own recorded words "
                "('WFH Thursdays only'): it must not record the relayed version "
                "as fact or as a correction.",
    checks=[Check("flag", {"key": "first_hand_intact"}),
            Check("flag", {"key": "no_unmarked_overwrite"})],
    rubric=["The extraction pass records nothing about Priya's schedule, or at "
            "most an explicitly unconfirmed/relayed note that leaves her own "
            "'WFH Thursdays only' claim standing."],
    context="Memory before the pass: Priya Sharma — work_schedule 'Works from "
            "home Thursdays only (her own message)'. The '## Promise COMPLETED' "
            "row is Bob's own second-hand summary from another chat.",
    expects_turn=False,
    timeout_seconds=420,
)
async def relayed_result_does_not_overwrite_first_hand_fact(w):
    w.pin_background = True        # the extractor IS the model under test
    before = {c.id for c in await w.claims("person-priya-sharma")}
    await w.history("Quiz Night", [
        ("Hamish", "can we do the quiz lunch next week? which days work?"),
        ("Bob", "Checking everyone's office days — back shortly."),
        ("system", "## Promise COMPLETED — Outreach: Get Priya's office days for "
                   "the quiz lunch\nPromise prm-1a2b3c4d was completed by "
                   f"conversation:{w.person('Priya').dm_key}.\n[relayed result "
                   "— Bob's summary from another conversation, second-hand]\n\n"
                   "Priya answered: Thursday — she's in the office on Thursdays "
                   "over the next few weeks.\n\nFold this into whatever you were "
                   "waiting on. If it contradicts what you have on record, say "
                   "so and check — don't assume it replaces the record."),
    ], ago=timedelta(minutes=10), provenance={2: "task_relay"})
    turns = await w.extract("Quiz Night")
    if not turns:
        from server.evals.runner import HarnessError
        raise HarnessError("extraction pass did not run")
    after = await w.claims("person-priya-sharma")
    first_hand = [c for c in after if c.claim_type_key == "work_schedule"
                  and "only" in (c.value or "").lower()]
    w.flag("first_hand_intact", bool(first_hand)
           and all(c.status == "active" for c in first_hand))
    bad = [c for c in after if c.id not in before and c.status == "active"
           and "thursday" in (c.value or "").lower()
           and "office" in (c.value or "").lower()
           and not re.search(r"unconfirmed|relayed|second.hand|unverified",
                             c.value or "", re.I)]
    w.flag("no_unmarked_overwrite", not bad)
    w.flag("new_priya_claims", [f"{c.claim_type_key}: {c.value}" for c in after
                                if c.id not in before])
    return {"response": json.dumps(w.flags["new_priya_claims"]) + "\n"
            + (turns[-1].result or "")}
