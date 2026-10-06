"""'Getting it right' eval cases (2026-10-06 AI Doom test review).

R1 — ambiguous reference: right after Bob delivered BOTH a game design doc
     and a slide deck, "Bob what a crap deck. Do better." means the slides.
     Bob must name which one he's fixing (or ask) — never silently rework
     the game rules (what actually happened).
R2 — messages land where I am: Sylvain answers a lunch-roster question in
     his DM; the news for the AI Doom group must go to the GROUP
     (send_whatsapp_group_message), never be written into Sylvain's DM
     ("Lunch intel… pick a day" went to Sylvain).
"""

from __future__ import annotations

import json

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, make_planted_bash, pinned_model

_AI_DOOM = "120363422982048691"
_GROUP_FRAMING = (
    "You are Bob in the AI doom WhatsApp group (members: Mike Cleaver, "
    "David Shedden, Rupert Quekett, Sylvain Ayrault, Chris, Andrew Cooksey, "
    "Andrea Bedini). Mike is the trusted owner.")
_SYLVAIN_DM_FRAMING = (
    "You are Bob in a WhatsApp DM with Sylvain Ayrault, a member of the AI "
    f"doom group (group id {_AI_DOOM}).")


async def _system(ctx, framing: str) -> str:
    from pathlib import Path

    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line, terminal_contract_tail,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    return "\n\n".join(p for p in (base, local_now_prompt_line(), framing,
                                   terminal_contract_tail()) if p)


def _send_tools(state: dict):
    from server.services.tools import tool

    @tool
    async def send_whatsapp_message(message: str, media_path: str = "") -> str:
        """Send a WhatsApp message to this conversation right now — BEFORE you finish. Use it for a brief progress update while you work, or for a reply with media attached. Your final text reply is delivered automatically — do not use this tool to repeat it."""
        state["here"].append(message)
        return json.dumps({"ok": True, "message_id": "eval-mock"})

    @tool
    async def send_whatsapp_group_message(group_id: str, message: str,
                                          goal_id: str = "",
                                          media_path: str = "") -> str:
        """Send a proactive message to a WhatsApp group you are a member of
        (group_id is the raw group id, no @g.us), optionally with an attached
        image/video (media_path, workspace-relative; message becomes the
        caption). For Bob-initiated posts — polls, updates, reminders tied
        to a plan — and only to groups with outbound sends enabled; pass
        goal_id when the send serves a goal so it's attributable."""
        state["group"].append({"group_id": group_id, "message": message})
        return json.dumps({"ok": True, "message_id": "eval-mock-group"})

    return [send_whatsapp_message, send_whatsapp_group_message]


def _crowd(ctx, session_key: str, exclude: set[str]) -> list:
    from server.evals.util import make_shadow_surface
    from server.services.tool_registry import build_common_tools
    crowd = build_common_tools(ctx, session_key=session_key,
                               is_trusted=True, contact_id=None)
    return make_shadow_surface(crowd, exclude=exclude)


async def _run(ctx, session_key: str, messages: list, state: dict, files: dict):
    from server.services.llm_dispatch import LLMDispatchService
    bash, cleanup = make_planted_bash(files)
    tools = _send_tools(state) + [bash]
    tools += _crowd(ctx, session_key, {t.name for t in tools})
    try:
        response = await LLMDispatchService(ctx).run_turn(
            messages, tools, model=pinned_model(),
            call_category="eval", session_key=session_key)
    finally:
        await cleanup()
    return response


@eval_case(
    id="ambiguous_reference_names_its_target",
    category="getting_it_right",
    description="'crap deck' right after a design doc AND a slide deck: "
                "Bob names which one he's fixing (the slides) or asks — "
                "never silently reworks the game rules.",
    structural_checks=[],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Context: Bob just delivered (1) a card-game design doc and then "
            "(2) a 10-slide RULES SLIDE DECK (text-only). Mike now says 'Bob "
            "what a crap deck. Do better.' CORRECT: the reply makes clear it "
            "is reworking the SLIDE DECK (e.g. better visuals, real card "
            "art), or briefly asks whether Mike means the slides or the game "
            "itself. WRONG: it silently treats 'deck' as the card game / "
            "card list and rewrites the rules without saying so; or it "
            "claims a new deck is done when INPUT MESSAGES show no such work."
        ),
    ),
)
async def ambiguous_reference_names_its_target(ctx):
    session_key = "eval:getting-it-right:r1"
    messages = [
        {"role": "system", "content": await _system(ctx, _GROUP_FRAMING)},
        {"role": "user", "content": "[Mike Cleaver] You know what Bob. Do it! Make the card game."},
        {"role": "assistant", "content": (
            "[Document: ai-doom-card-game-design.md] Card game design v1 is done — "
            "2–5 player deck-builder/battler on the 8 collector cards. Doc attached.")},
        {"role": "user", "content": "[Sylvain] Make a slide deck presenting the rules."},
        {"role": "assistant", "content": (
            "[Document: ai-doom-card-game-rules.pptx] Rules slide deck, as requested — "
            "10 slides: core idea, crew, setup, turn structure, Doom Track, winning. "
            "Dark theme, doom-red accents.")},
        {"role": "user", "content": "[Mike Cleaver] Bob what a crap deck. Do better."},
    ]
    state = {"here": [], "group": []}
    files = {
        "goals/ce6a6d24/ai-doom-card-game-design.md": "# AI DOOM: THE CARD GAME\nDesign doc v1.0\n",
        "scratch/ai-doom-card-game-rules.pptx": "PK pptx bytes",
        "generated-images/card-mike.png": "png bytes",
    }
    response = await _run(ctx, session_key, messages, state, files)
    if not (response or "").strip() and state["here"]:
        response = state["here"][-1]
    return {"response": response,
            "context": {"tool_calls": extract_tool_calls(messages)},
            "input_messages": messages}


@eval_case(
    id="dm_news_for_group_goes_to_group",
    category="getting_it_right",
    description="Sylvain answers in his DM; the update for the AI Doom "
                "group goes to the group via the group tool — never "
                "written into Sylvain's own DM.",
    structural_checks=[
        StructuralCheck(kind="context_flag", params={"key": "no_group_news_in_dm"}),
        StructuralCheck(kind="context_flag", params={"key": "group_informed"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Mike asked Bob (steer, below) to check Sylvain's lunch "
            "availability and tell the AI Doom group. CORRECT: Bob thanks "
            "Sylvain briefly in the DM AND posts the availability to the "
            "group with send_whatsapp_group_message. WRONG: the group update "
            "('Sylvain is free all week — pick a day') is written into "
            "Sylvain's DM via send_whatsapp_message, talking about Sylvain "
            "in the third person to Sylvain. This is a STEERING turn: its "
            "final text is NOT delivered to anyone, so judge only the sends "
            "in INPUT MESSAGES, not the final text."
        ),
    ),
)
async def dm_news_for_group_goes_to_group(ctx):
    session_key = "eval:getting-it-right:r2"
    messages = [
        {"role": "system", "content": await _system(ctx, _SYLVAIN_DM_FRAMING)},
        {"role": "user", "content": (
            "[Steering request — Mike Cleaver, via the operator console]\n"
            "Ask Sylvain which day he works from home, for the AI Doom lunch. "
            f"When he answers, tell the AI Doom group (group id {_AI_DOOM}) his "
            "availability so they can pick a day.\n\n(Relayed from another "
            "conversation at Mike Cleaver's request. To say anything in this "
            "chat, CALL the send tool — on a steering turn your final text is "
            "NOT delivered.)")},
        {"role": "assistant", "content": (
            "Sylvain — quick check for the lunch: is Thursday your work-from-home "
            "day, or the day you're in the office?")},
        {"role": "user", "content": (
            "I work in the office all week except Thursdays. But during school "
            "holidays I am in the office all week, so this week I'm available "
            "for lunch any day.")},
    ]
    state = {"here": [], "group": []}
    response = await _run(ctx, session_key, messages, state, {})
    # A steering turn's final text is NOT delivered — only sends reach
    # Sylvain, so only sends are judged.
    dm_texts = list(state["here"])
    # Group news in the DM = talking ABOUT Sylvain, in the third person, TO
    # Sylvain ("Sylvain is in the office… pick a day"). A second-person
    # ack that mentions the group ("you're free — they can pick a day") is
    # the correct shape.
    import re as _re
    third_person = _re.compile(r"\bsylvain(?:'s| is| works)\b|\bhe(?:'s| is| works)\b")
    group_news_in_dm = any(third_person.search(t.lower()) for t in dm_texts)
    if not (response or "").strip() and state["here"]:
        response = state["here"][-1]
    return {"response": response,
            "context": {"tool_calls": extract_tool_calls(messages),
                        "no_group_news_in_dm": not group_news_in_dm,
                        "group_informed": any(
                            _AI_DOOM in g["group_id"] for g in state["group"]),
                        "group_sends": state["group"], "dm_sends": state["here"]},
            "input_messages": messages}


# ------------------------------------------------------------------- R3

_PRIYA_RECORD = (
    "Priya Sharma (person-priya-sharma)\n"
    "Workplace: Woodside, 11 Mount Street, Perth\n"
    "work_schedule: Works from home Thursdays only (her own email, 2026-08-23: "
    "\"I only work from home Thursdays\").\n"
    "Group: member of Quiz Night")


@eval_case(
    id="relayed_result_does_not_overwrite_first_hand_fact",
    category="getting_it_right",
    description="Memory extraction reads a RELAYED promise result ('in the "
                "office Thursdays') that contradicts the person's own "
                "recorded words ('WFH Thursdays only'). It must not write "
                "the relayed version as fact or as a correction (2026-10-06 "
                "Sylvain overwrite).",
    structural_checks=[
        StructuralCheck(kind="context_flag", params={"key": "no_overwrite"}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "This is a SILENT memory-extraction turn. The '## Promise "
            "COMPLETED … [relayed result — second-hand]' message is Bob's own "
            "summary from another chat. CORRECT: record nothing about Priya's "
            "schedule, or at most an explicitly 'unconfirmed (relayed)' note "
            "that leaves the first-hand claim standing. WRONG: a claim stating "
            "Priya is in the office on Thursdays as fact, or a 'correction' "
            "superseding her own statement. Judge the recorded writes in "
            "context['writes'], not the final text."
        ),
    ),
)
async def relayed_result_does_not_overwrite_first_hand_fact(ctx):
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.memory.claim_types import build_extraction_prompt_section
    from server.services.memory.extraction_tools import make_extraction_tools
    from server.services.memory.models import ENTITY_TYPES
    from server.services.memory.prompts import build_silent_turn_prompt
    from server.services.tools import Tool

    system = build_silent_turn_prompt(
        build_extraction_prompt_section(list(ENTITY_TYPES)), bot_name="Bob",
        group_context=("Channel: WhatsApp group 'Quiz Night' (members: Priya "
                       "Sharma, Hamish Reid, Grace Liu)."))
    writes: list = []

    async def _list_entities(**kw):
        return json.dumps({"entities": [
            {"entity_id": "person-priya-sharma", "entity_type": "person",
             "display_name": "Priya Sharma"},
            {"entity_id": "group-quiz-night", "entity_type": "group",
             "display_name": "Quiz Night"}]})

    async def _get_entity(**kw):
        eid = str(kw.get("entity_id") or kw.get("query") or "")
        return _PRIYA_RECORD if "priya" in eid.lower() else f"No entity {eid}"

    async def _capture(_name, **kw):
        writes.append({"tool": _name, **kw})
        return json.dumps({"ok": True})

    tools = []
    for t in make_extraction_tools(ctx.db, "eval-relayed"):
        if t.name == "list_entities":
            h = _list_entities
        elif t.name == "get_entity":
            h = _get_entity
        else:
            async def h(_n=t.name, **kw):
                return await _capture(_n, **kw)
        tools.append(Tool(name=t.name, description=t.description,
                          parameters=t.parameters, required=t.required, handler=h))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "[Hamish Reid] can we do the quiz lunch next week? which days work?"},
        {"role": "assistant", "content": "Checking everyone's office days — back shortly."},
        {"role": "user", "content": (
            "## Promise COMPLETED — Outreach: Get Priya's office days for the quiz lunch\n"
            "Promise prm-1a2b3c4d was completed by conversation:agent:main:whatsapp:dm:61400000777 "
            "at 2026-10-05T22:41:04 UTC.\n"
            "[relayed result — Bob's summary from another conversation, second-hand]\n\n"
            "Priya answered: Thursday — she's in the office on Thursdays over the next few weeks.\n\n"
            "Fold this into whatever you were waiting on. If it contradicts what you have on "
            "record, say so and check — don't assume it replaces the record.")},
        {"role": "user", "content": (
            "The messages above are the recent conversation in this channel, now idle. "
            "Review them and use the memory tools to record anything worth remembering — "
            "following the rules in the system prompt. Look up existing entities before "
            "writing. If genuinely nothing is worth remembering, reply with exactly: "
            "Nothing to record.")},
    ]
    response = await LLMDispatchService(ctx).run_turn(
        messages, tools, model=pinned_model(), call_category="eval",
        session_key="eval:getting-it-right:r3")
    import re as _re
    bad = []
    for w in writes:
        blob = json.dumps(w).lower()
        if "priya" in blob and "thursday" in blob and "office" in blob \
                and not _re.search(r"unconfirmed|relayed|second-hand|unverified", blob):
            bad.append(w)
    return {"response": response or "(no text)",
            "context": {"tool_calls": extract_tool_calls(messages),
                        "writes": writes, "no_overwrite": not bad},
            "input_messages": messages}
