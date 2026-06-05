"""Voice chat eval cases."""

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case


async def _build_voice_messages(
    ctx,
    user_text: str,
    *,
    voice_instructions_extra: str = "",
) -> list[dict]:
    """Build messages matching production voice_service message assembly."""
    from cyborg_server.services.prompt_assembler import load_workspace_prompt

    workspace = await load_workspace_prompt(ctx.settings.harness.workspace_dir, db=getattr(ctx, 'db', None))

    voice_instructions = (
        "You are participating in a live voice conversation. "
        "Respond in plain spoken language: no emojis, no markdown formatting, "
        "no asterisks, no bullet points. Just natural speech."
    )
    if voice_instructions_extra:
        voice_instructions += f"\n\n{voice_instructions_extra}"

    system_parts: list[str] = []
    if workspace:
        system_parts.append(workspace)
    system_parts.append(voice_instructions)

    messages = [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": user_text},
    ]
    return messages


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
            "numbered lists, or overly formal language. It should feel conversational. "
            "Evaluate in the context of the system prompt persona — if the persona "
            "favours brevity and directness, concise replies are correct."
        ),
    ),
)
async def voice_chat_concise(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = await _build_voice_messages(ctx, "Hey, how are you doing today?")

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(messages, call_category="eval")
    return {"response": response, "input_messages": messages}


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
            "as instructed by the agenda. Off-topic responses score low. "
            "Evaluate in the context of the system prompt persona."
        ),
    ),
)
async def voice_chat_agenda(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = await _build_voice_messages(
        ctx,
        "I need to set up a team sync sometime soon.",
        voice_instructions_extra=(
            "CALL AGENDA: Help the user schedule a meeting for next week. "
            "Follow this agenda throughout the conversation. Stay on topic "
            "and work toward the agenda's goal."
        ),
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(messages, call_category="eval")
    return {"response": response, "input_messages": messages}


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
            "Score low if the response is entirely in English with no French. "
            "Evaluate in the context of the system prompt persona."
        ),
    ),
)
async def voice_chat_language_coach(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = await _build_voice_messages(
        ctx,
        "Bonjour, je voudrais practise mon francais.",
        voice_instructions_extra=(
            "Respond in French. Act as a language coach: suggest corrections to "
            "the user's grammar and phrasing when they make mistakes."
        ),
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(messages, call_category="eval")
    return {"response": response, "input_messages": messages}
