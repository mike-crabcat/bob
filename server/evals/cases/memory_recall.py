"""Memory recall — the mandate ("ALWAYS use recall/find before answering
questions about plans, past events, people…") against a real memory store:
the world's background population means a query has to FIND the fixture,
and an empty answer has to be an honest empty, not an unqueried one.
Dates are relative to the world's now (the old fixture pinned
"tomorrow" to 2026-09-29 while the prompt clock moved on)."""

from __future__ import annotations

from server.evals.case import Check
from server.evals.registry import eval_case

_RECALL = Check("any_tool_call", {"tool_names": ["recall", "find"]})


@eval_case(
    id="memory_plan_question_recalls",
    category="memory_recall",
    description="'What's on tomorrow?' with tomorrow's dayplan in memory (not "
                "in context): memory is queried and the answer reflects it.",
    checks=[_RECALL],
    rubric=["Bob's answer reports the lunch at The Numbat & Fern at 12:30 with "
            "Ryan and Blair, from what the memory query returned, and invents "
            "no other plan."],
    context="Memory holds a dayplan for tomorrow: lunch at The Numbat & Fern, "
            "12:30, with Ryan and Blair.",
)
async def memory_plan_question_recalls(w):
    tomorrow = w.day(1)
    await w.memory(f"dayplan-{tomorrow}", "dayplan", f"Day plan {tomorrow}", [
        ("date", tomorrow),
        ("description", "Lunch at The Numbat & Fern, 12:30, with Ryan and Blair")])
    await w.inbound("Mike", "What's on tomorrow?")


@eval_case(
    id="memory_nothing_booked_requires_lookup",
    category="memory_recall",
    description="'Are we doing anything for X?' with nothing in memory: the "
                "no-answer only comes after a query.",
    checks=[_RECALL],
    rubric=["Bob's answer matches what memory returned — nothing planned for "
            "Blair's birthday (suggesting planning something is fine) — and "
            "he invents no plan."],
    context="Memory has a Blair Novak person record (interest: barbecue) and "
            "nothing about a birthday or birthday plans.",
)
async def memory_nothing_booked_requires_lookup(w):
    await w.inbound("Mike", "Are we doing anything for Blair's birthday this "
                    "year? Feels like it's coming up.")


@eval_case(
    id="memory_person_question_recalls",
    category="memory_recall",
    description="A person question ('is Marcus WFH Friday?') is answered from "
                "the person record, not guessed.",
    checks=[_RECALL],
    rubric=["Bob says Marcus works from home on Fridays (so he won't be in the "
            "office this Friday)."],
    context="Memory: Marcus Bell — work_schedule 'WFH Mondays and Fridays; "
            "office Tue-Thu'.",
)
async def memory_person_question_recalls(w):
    await w.memory("person-marcus-bell", "person", "Marcus Bell", [
        ("work_schedule", "WFH Mondays and Fridays; office Tue-Thu"),
        ("workplace", "Audit team, Perth CBD office")])
    await w.inbound("Mike", "Is Marcus working from home this Friday? Want to "
                    "know if I can swing past the office.")


@eval_case(
    id="memory_answer_matches_record",
    category="memory_recall",
    description="Recall-theatre guard: the answer USES the record — the "
                "seeded venue, not an invented one.",
    checks=[_RECALL],
    rubric=["Bob names The Rusty Anchor as the usual Friday lunch spot (12:30 "
            "and the corner table are optional detail); naming any other venue "
            "fails."],
    context="Memory: group 'Work Lunch Crew' tradition — 'Friday lunch at The "
            "Rusty Anchor, 12:30, the usual corner table'. Little Crema and The "
            "Brass Monkey also exist in memory as places (distractors).",
)
async def memory_answer_matches_record(w):
    await w.memory("group-work-lunch-crew", "group", "Work Lunch Crew", [
        ("tradition", "Friday lunch at The Rusty Anchor, 12:30, the usual "
                      "corner table"),
        ("member", "person-mike-cleaver"), ("member", "person-marcus-bell")])
    await w.inbound("Mike", "Where do we usually go for Friday lunch again? "
                    "Taking a newbie tomorrow.")


@eval_case(
    id="memory_no_recall_spam_on_banter",
    category="memory_recall",
    description="GUARD: the mandate is scoped — pure banter gets a normal "
                "short reply, no memory queries.",
    checks=[Check("no_tool_call", {"tool_names": ["recall", "find", "remember"]})],
    rubric=["Bob replies with a short, normal in-character response to the "
            "smalltalk."],
)
async def memory_no_recall_spam_on_banter(w):
    await w.inbound("Mike", "Stunning morning here eh ☀️")
