"""Voice chat eval cases."""

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case


@eval_case(
    id="voice_chat_concise_response",
    category="voice_chat",
    description="Voice chat should respond in plain spoken language without markdown.",
    structural_checks=[
        StructuralCheck(kind="min_length", params={"min_length": 10}),
        StructuralCheck(kind="max_length", params={"max_length": 500}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The response should sound like natural spoken language. "
            "Deduct points for markdown formatting (**, *, #), bullet points, "
            "numbered lists, or overly formal language. It should feel conversational."
        ),
    ),
)
async def voice_chat_concise(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": (
            "You are participating in a live voice conversation. "
            "Respond in plain spoken language: no emojis, no markdown formatting, "
            "no asterisks, no bullet points. Just natural speech."
        )},
        {"role": "user", "content": "Hey, how are you doing today?"},
    ]

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(messages, call_category="eval")
    return {"response": response}


@eval_case(
    id="voice_chat_agenda_following",
    category="voice_chat",
    description="Voice chat with an agenda should stay on topic.",
    structural_checks=[
        StructuralCheck(kind="min_length", params={"min_length": 20}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The response should address scheduling a meeting "
            "as instructed by the agenda. Off-topic responses score low."
        ),
    ),
)
async def voice_chat_agenda(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": (
            "You are participating in a live voice conversation. "
            "Respond in plain spoken language: no emojis, no markdown formatting, "
            "no asterisks, no bullet points. Just natural speech.\n\n"
            "CALL AGENDA: Help the user schedule a meeting for next week."
        )},
        {"role": "user", "content": "I need to set up a team sync sometime soon."},
    ]

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(messages, call_category="eval")
    return {"response": response}


@eval_case(
    id="voice_chat_language_coach",
    category="voice_chat",
    description="Voice chat as language coach should respond in target language.",
    structural_checks=[
        StructuralCheck(kind="min_length", params={"min_length": 15}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The response should primarily be in French since the user is learning French. "
            "It should act as a language coach, possibly correcting the user's French. "
            "Score low if the response is entirely in English with no French."
        ),
    ),
)
async def voice_chat_language_coach(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {"role": "system", "content": (
            "You are participating in a live voice conversation. "
            "Respond in plain spoken language: no emojis, no markdown formatting, "
            "no asterisks, no bullet points. Just natural speech.\n\n"
            "Respond in French. Act as a language coach: suggest corrections to "
            "the user's grammar and phrasing when they make mistakes."
        )},
        {"role": "user", "content": "Bonjour, je voudrais practise mon francais."},
    ]

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(messages, call_category="eval")
    return {"response": response}
