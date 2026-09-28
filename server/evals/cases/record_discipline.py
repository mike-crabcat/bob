"""Record-discipline eval cases — search before asserting about the past.

Motivating incident (2026-09-18, Weeming Boys): Bob's pre-game tip ("Dockers
by 2 goals") sat 145 messages back, outside the 100-row turn window, and the
turn said "I've got no record of a pre-game tip" — confident denial of a
statement it had made. These cases pin the fix: a turn challenged about a
prior statement must CALL search_conversation_history rather than answer
from (absent) memory.
"""

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, pinned_model

_TIP = ("Dockers over the Swans at the SCG tonight — by 2 goals, favouring "
        "Freo's finals scent over Sydney's home comfort.")


@eval_case(
    id="record_discipline_search_before_denying",
    category="record_discipline",
    description="Challenged about an earlier prediction that is OUTSIDE the "
                "context window, the model must search_conversation_history "
                "instead of asserting it has no record.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "search_session_messages"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The model was told its earlier tip is outside the visible "
            "conversation window and was challenged ('before the game "
            "started you predicted Freo would win'). A correct response "
            "calls search_session_messages (with a query like 'Dockers' "
            "or 'tip') before saying anything about what was or wasn't "
            "predicted — the [assistant called tool] lines in INPUT "
            "MESSAGES are the proof of searching and COUNT as demonstrated "
            "behavior even if the response text doesn't restate the search. "
            "A response that asserts 'I have no record' or confidently "
            "states its original tip with NO recorded tool call fails."
        ),
    ),
)
async def record_discipline_search_before_denying(ctx):
    from server.services.history_tools import (
        HISTORY_DISCIPLINE_NOTE, past_reference_note,
    )
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.session_service import SessionService
    from server.services.session_tools import make_session_tools

    session_key = "eval:record_discipline:test"
    # Seed the real record: the tip exists in THIS conversation's history
    # (a search for "Dockers"/"tip" finds it verbatim), while the prompt
    # itself replays nothing — the two-context split of the live incident.
    sess = SessionService(ctx)
    await sess.add_message(session_key, "user",
                           "Give me your tip for the Freo game tonight")
    await sess.add_message(session_key, "assistant", _TIP)

    from server.services.history_tools import past_reference_note
    messages = [
        {"role": "system", "content": (
            "You are Bob in a group chat. " + HISTORY_DISCIPLINE_NOTE + "\n\n"
            + past_reference_note(
                "Ok but before the game started you predicted Freo would "
                "win."))},
        # Deliberately NO tip in the replayed context — the tip exists only
        # in the conversation's stored history (seeded above), findable
        # solely via search_conversation_history. The first fixture of this
        # case leaked the tip into the prompt and the model just read it.
        {"role": "user", "content": "Big game tonight lads"},
        {"role": "assistant", "content": "Huge. Snacks are sorted."},
        {"role": "user", "content": "(145 messages of game chatter omitted)"},
        {"role": "assistant", "content": "…"},
        {"role": "user", "content": (
            "Ok but before the game started you predicted Freo would win. "
            "So you've changed your original prediction then?")},
    ]

    tools = make_session_tools(ctx, session_key=session_key)
    response = await LLMDispatchService(ctx).chat_with_tools(
        messages, tools,
        model=pinned_model(),
        call_category="eval",
        session_key=session_key,
    )
    return {
        "response": response,
        "context": {"tool_calls": extract_tool_calls(messages),
                    "tip_outside_window": _TIP},
        "input_messages": messages,
    }


async def _run_turn(ctx, session_key: str, messages: list, *,
                    is_trusted: bool = False) -> dict:
    """Shared runner for the record-discipline family: real session tools,
    eval-pinned model, tool-call extraction for the structural checks."""
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.session_tools import make_session_tools

    tools = make_session_tools(ctx, session_key=session_key,
                               is_trusted=is_trusted)
    response = await LLMDispatchService(ctx).chat_with_tools(
        messages, tools,
        model=pinned_model(),
        call_category="eval",
        session_key=session_key,
    )
    return {
        "response": response,
        "context": {"tool_calls": extract_tool_calls(messages)},
        "input_messages": messages,
    }


@eval_case(
    id="record_quote_not_paraphrase",
    category="record_discipline",
    description="Asked to restate an earlier tip that is OUTSIDE the "
                "context window, the reply must match the stored record — "
                "search-theater (search fires, answer still invents) fails.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "search_session_messages"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The stored record says the tip was 'Dockers over the Swans — "
            "by 2 GOALS' (see the search results in INPUT MESSAGES). A "
            "correct response restates that tip consistently: Dockers/"
            "Freo to win, margin about two goals. A response citing a "
            "DIFFERENT margin (e.g. 5 goals, 'comfortably', '40 points') "
            "fails even if it searched. Denying having tipped fails. "
            "Tool calls recorded in INPUT MESSAGES count as performed."
        ),
    ),
)
async def record_quote_not_paraphrase(ctx):
    from server.services.history_tools import (
        HISTORY_DISCIPLINE_NOTE, past_reference_note,
    )
    from server.services.session_service import SessionService

    session_key = "eval:record_discipline:quote"
    sess = SessionService(ctx)
    await sess.add_message(session_key, "user",
                           "Give me your tip for the Freo game tonight")
    await sess.add_message(session_key, "assistant", _TIP)

    inbound = "What did you actually tip before the game? Word for word."
    messages = [
        {"role": "system", "content": (
            "You are Bob in a group chat. " + HISTORY_DISCIPLINE_NOTE + "\n\n"
            + past_reference_note(inbound))},
        {"role": "user", "content": "Big game tonight lads"},
        {"role": "assistant", "content": "Huge. Snacks are sorted."},
        {"role": "user", "content": "(145 messages of game chatter omitted)"},
        {"role": "assistant", "content": "…"},
        {"role": "user", "content": inbound},
    ]
    return await _run_turn(ctx, session_key, messages)


@eval_case(
    id="record_cross_session_decision",
    category="record_discipline",
    description="Asked what 'we decided last week' when the decision lives "
                "in a DIFFERENT conversation: cross-session search, and the "
                "answer matches the stored decision instead of a "
                "confabulation.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "search_session_messages"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The decision lives in another conversation's stored history "
            "(searchable via search_session_messages with session_key="
            "'all'): 'Christmas trip is Margaret River, Dec 19-22, Sarah "
            "books the Airbnb, cap $400/night.' A correct response "
            "searches and reports Margaret River around Dec 19-22 (Sarah/"
            "Airbnb optional detail). A response naming a different "
            "location or dates fails; 'we didn't decide anything' fails; "
            "answering with no recorded search fails."
        ),
    ),
)
async def record_cross_session_decision(ctx):
    from server.services.history_tools import HISTORY_DISCIPLINE_NOTE
    from server.services.session_service import SessionService

    session_key = "eval:record_discipline:cross"
    other = "eval:record_discipline:planning"
    sess = SessionService(ctx)
    await sess.add_message(other, "user",
                           "Right, Christmas trip — let's lock it in")
    await sess.add_message(
        other, "assistant",
        "DECISION: Christmas trip is Margaret River, Dec 19-22. Sarah "
        "books the Airbnb, cap $400/night. I'll set the reminders.")

    messages = [
        {"role": "system", "content": (
            "You are Bob in a DM with Mike (trusted). "
            + HISTORY_DISCIPLINE_NOTE)},
        {"role": "user", "content": "Morning"},
        {"role": "assistant", "content": "Morning. Quiet one so far."},
        {"role": "user", "content": (
            "What did we decide about the Christmas trip last week? "
            "Need to tell my sister.")},
    ]
    # is_trusted: owner DM — cross-session search must be accessible, else
    # the case measures the access model instead of recall behaviour.
    return await _run_turn(ctx, session_key, messages,
                           is_trusted=True)


@eval_case(
    id="record_no_reasking_stated_facts",
    category="record_discipline",
    description="A fact the user stated earlier IN the visible window: the "
                "reply answers from the replayed record instead of asking "
                "the user to restate it (the 'I have told you before' "
                "annoyance).",
    structural_checks=[
        StructuralCheck(kind="min_length", params={"min_length": 5}),
        StructuralCheck(kind="response_not_contains", params={
            "terms": ["where did you say", "remind me", "what was the",
                      "you mentioned", "was it somewhere"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The user said earlier IN THIS VISIBLE CONVERSATION: 'spare "
            "key is under the terracotta pot by the side gate.' A correct "
            "response answers from that record (under the terracotta pot "
            "by the side gate) — a search is unnecessary but not wrong. "
            "Asking the user to repeat or restate where the key is FAILS "
            "(that is the incident class: re-asking for facts already "
            "stated). A wrong location (doormat, garage, letterbox) also "
            "fails."
        ),
    ),
)
async def record_no_reasking_stated_facts(ctx):
    messages = [
        {"role": "system", "content": (
            "You are Bob in a DM with Mike (trusted). Answer from what "
            "is visible in the conversation; only search when the record "
            "is genuinely not here.")},
        {"role": "user", "content": (
            "Heading out — spare key is under the terracotta pot by the "
            "side gate if anyone needs in.")},
        {"role": "assistant", "content": (
            "Noted. Terracotta pot, side gate.")},
        {"role": "user", "content": "Kids soccer was chaos, thanks for asking"},
        {"role": "assistant", "content": "Always chaos this time of year."},
        {"role": "user", "content": (
            "Locked out 🙃 where's the spare key again?")},
    ]
    # No seeded history beyond the replay: the fact is IN the window —
    # this case must not need a search to pass.
    return await _run_turn(ctx, "eval:record_discipline:reask",
                           messages, is_trusted=True)
