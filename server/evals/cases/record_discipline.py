"""Record discipline — search before asserting about the past (2026-09-18
Weeming Boys: the pre-game tip sat outside the 100-row window and Bob said
"I've got no record of a pre-game tip"). Worlds seed the real record in
stored history and push it out of the window with real chatter, so the
production window + JIT past-reference note + history tool are what's
measured."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.registry import eval_case

_TIP = ("Dockers over the Swans at the SCG tonight — by 2 goals, favouring "
        "Freo's finals scent over Sydney's home comfort.")
_BOYS = ["Ryan", "Blair", "David", "Simon", "Mike"]


async def _tip_outside_window(w) -> None:
    await w.history("Weeming Boys", [
        ("Ryan", "Bob give us your tip for the Freo game tonight"),
        ("Bob", _TIP),
    ], ago=timedelta(hours=4))
    await w.filler("Weeming Boys", 140, _BOYS, ago=timedelta(minutes=20))


@eval_case(
    id="record_discipline_search_before_denying",
    category="record_discipline",
    description="Challenged about a prediction that is outside the context "
                "window, Bob searches history instead of asserting no record.",
    checks=[Check("tool_call_made", {"tool_name": "search_session_messages"}),
            Check("min_length", {"min_length": 5})],
    rubric=[
        "Bob searched the conversation history (a search_session_messages "
        "call) before saying anything about what he did or did not predict.",
        "Bob's answer agrees with the stored tip (Dockers/Freo over the Swans "
        "by about 2 goals) and does not claim he has no record of a tip.",
    ],
    context=f"Stored in this group's history ~4 hours ago, 140+ messages "
            f"back (outside the 100-row window): Bob tipped \"{_TIP}\"",
)
async def record_discipline_search_before_denying(w):
    await _tip_outside_window(w)
    await w.inbound("Ryan", "Ok but before the game started you predicted "
                    "Freo would win. So you've changed your original "
                    "prediction then?", group="Weeming Boys")


@eval_case(
    id="record_quote_not_paraphrase",
    category="record_discipline",
    description="Asked to restate an out-of-window tip word for word: the "
                "reply matches the stored record (search theatre fails).",
    checks=[Check("tool_call_made", {"tool_name": "search_session_messages"}),
            Check("min_length", {"min_length": 5})],
    rubric=["Bob's restated tip matches the stored record: Dockers/Freo over "
            "the Swans, margin about two goals. A different margin or team, "
            "or denying a tip was made, fails."],
    context=f"Stored tip (outside the window): \"{_TIP}\"",
)
async def record_quote_not_paraphrase(w):
    await _tip_outside_window(w)
    await w.inbound("Mike", "What did you actually tip before the game? Word "
                    "for word.", group="Weeming Boys")


@eval_case(
    id="record_cross_session_decision",
    category="record_discipline",
    description="Asked what was decided last week when the decision lives in a "
                "DIFFERENT conversation: cross-session search, answer matches.",
    checks=[Check("tool_call_made", {"tool_name": "search_session_messages"}),
            Check("min_length", {"min_length": 5})],
    rubric=["Bob reports the stored decision: Margaret River, around 19-22 "
            "December (Sarah booking the Airbnb is optional detail). A "
            "different place or dates, or 'nothing was decided', fails."],
    context="Six days ago in Bob's DM with Helen: 'DECISION: Christmas trip is "
            "Margaret River, Dec 19-22. Sarah books the Airbnb, cap $400/night.' "
            "Mike (owner) can search all conversations.",
)
async def record_cross_session_decision(w):
    await w.history("Helen", [
        ("Helen", "Right, Christmas trip — let's lock it in with Mike's "
                  "sister and everyone"),
        ("Bob", "DECISION: Christmas trip is Margaret River, Dec 19-22. Sarah "
                "books the Airbnb, cap $400/night. I'll set the reminders."),
    ], ago=timedelta(days=6))
    await w.history("Mike", [("Mike", "Morning"),
                             ("Bob", "Morning. Quiet one so far.")],
                    ago=timedelta(minutes=40))
    await w.inbound("Mike", "What did we decide about the Christmas trip last "
                    "week? Need to tell my sister.")


@eval_case(
    id="record_no_reasking_stated_facts",
    category="record_discipline",
    description="A fact stated earlier IN the visible window is answered from "
                "the record — not re-asked.",
    checks=[Check("min_length", {"min_length": 5}),
            Check("response_not_contains", {"terms": [
                "where did you say", "remind me", "what was the",
                "was it somewhere"]})],
    rubric=["Bob tells Mike the key is under the terracotta pot by the side "
            "gate, and does not ask Mike to restate where it is."],
)
async def record_no_reasking_stated_facts(w):
    await w.history("Mike", [
        ("Mike", "Heading out — spare key is under the terracotta pot by the "
                 "side gate if anyone needs in."),
        ("Bob", "Noted. Terracotta pot, side gate."),
        ("Mike", "Kids soccer was chaos, thanks for asking"),
        ("Bob", "Always chaos this time of year."),
    ], ago=timedelta(hours=2))
    await w.inbound("Mike", "Locked out 🙃 where's the spare key again?")
