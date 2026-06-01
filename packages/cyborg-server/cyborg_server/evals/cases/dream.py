"""Dream eval cases: test that the dream LLM correctly curates bulletins into memory entries."""

import json

from cyborg_server.evals.case import JudgeCriteria, StructuralCheck
from cyborg_server.evals.registry import eval_case

_BULLETIN_DAVID_FAMILY = (
    "- Mike stated that David has a wife called Ela, a son called Orion, "
    "a new baby, two cats, lives in Mount Lawley, and drives a Subaru and an e-bike."
)

_BULLETIN_SYLVAIN_PREFS = (
    "- Sylvain prefers email communication over WhatsApp\n"
    "- Sylvain is interested in workspace automation and cron scheduling\n"
    "- Sylvain enjoys playful banter"
)

_BULLETIN_SINGLE_FACT = (
    "- Helen goes to Next Gen Kings Park gym near Kings Park"
)

_EXISTING_ENTRIES_COMPACT = (
    "[people/david-shedden] David Shedden\n"
    "[people/mike] Mike\n"
    "[people/sylvain] Sylvain\n"
    "[facts/helen-gym-near-kings-park] Helen's gym near Kings Park\n"
)


def _build_dream_messages(
    bulletins: list[dict],
    existing_entries: str = "",
) -> list[dict]:
    """Build the messages array that the dream process would send to the LLM."""
    from cyborg_server.services.memory.prompts import ENTITY_UPDATE_PROMPT

    system_prompt = ENTITY_UPDATE_PROMPT

    bulletin_lines: list[str] = []
    for i, b in enumerate(bulletins, 1):
        header = f"[{i}] {b['slug']}"
        if b.get("source_session"):
            header += f" (session: {b['source_session']}"
            if b.get("time_window"):
                header += f", window: {b['time_window']}"
            header += ")"
        bulletin_lines.append(f"{header}\n{b['content']}")

    user_prompt = "## NEW CLAIMS\n\n" + "\n\n".join(bulletin_lines)
    if existing_entries:
        user_prompt += "\n\n## EXISTING ENTITIES\n\n" + existing_entries

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _parse_operations(response: str) -> list[dict]:
    """Parse the JSON array from the LLM response."""
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        ops = json.loads(text)
        return ops if isinstance(ops, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


@eval_case(
    id="dream_people_update",
    category="dream",
    description="Dream should update people entries from bulletin with family details.",
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
        StructuralCheck(
            kind="json_schema",
            params={"required_fields": ["action", "category", "slug", "title", "content"], "array_field": "ops"},
            description="Each operation must have action, category, slug, title, content",
        ),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should produce at least one write operation for the 'people' category "
            "about David Shedden. The entry should contain details about his wife Ela, "
            "son Orion, new baby, cats, location (Mount Lawley), and vehicles (Subaru, e-bike). "
            "The content should use section headers (like Family, Work, etc.) and include "
            "transcript reference tags like [[session:...]]. "
            "It should NOT return an empty array — there is clear factual content to extract."
        ),
    ),
)
async def dream_people_update(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = _build_dream_messages(
        bulletins=[
            {
                "slug": "blt-test001",
                "source_session": "whatsapp:contact:mike123",
                "time_window": "2026-05-24T10:00..10:30",
                "content": _BULLETIN_DAVID_FAMILY,
            },
        ],
        existing_entries=_EXISTING_ENTRIES_COMPACT,
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(
        messages=messages,
        call_category="eval_dream",
        temperature=0.4,
        max_tokens=2000,
    )

    return {"response": response, "input_messages": messages}


@eval_case(
    id="dream_new_person_entry",
    category="dream",
    description="Dream should create a new person entry when no existing entry matches.",
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should create a new 'people' entry for Sylvain using the bulletin content. "
            "The entry should include Interests, Preferences sections with the details provided. "
            "It should NOT return an empty array."
        ),
    ),
)
async def dream_new_person_entry(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = _build_dream_messages(
        bulletins=[
            {
                "slug": "blt-test002",
                "source_session": "email:thread:sylvain-hello",
                "time_window": "2026-05-23T14:00..14:30",
                "content": _BULLETIN_SYLVAIN_PREFS,
            },
        ],
        existing_entries=(
            "[people/mike] Mike\n"
            "[people/david-shedden] David Shedden\n"
        ),
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(
        messages=messages,
        call_category="eval_dream",
        temperature=0.4,
        max_tokens=2000,
    )

    return {"response": response, "input_messages": messages}


@eval_case(
    id="dream_update_existing_fact",
    category="dream",
    description="Dream should update an existing fact entry with new info, not ignore it.",
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The LLM should update the existing helen-gym-near-kings-park entry or create a new one "
            "with the same information. It should NOT return an empty array — there is a clear factual "
            "claim in the bulletin about Helen's gym."
        ),
    ),
)
async def dream_update_existing_fact(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    messages = _build_dream_messages(
        bulletins=[
            {
                "slug": "blt-test003",
                "source_session": "whatsapp:contact:mike123",
                "time_window": "2026-05-24T09:00..09:15",
                "content": _BULLETIN_SINGLE_FACT,
            },
        ],
        existing_entries=_EXISTING_ENTRIES_COMPACT,
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(
        messages=messages,
        call_category="eval_dream",
        temperature=0.4,
        max_tokens=2000,
    )

    return {"response": response, "input_messages": messages}


@eval_case(
    id="dream_hedged_bulletin",
    category="dream",
    description="Dream should extract facts even from bulletins with hedging language.",
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The bulletin contains hedging language ('unverified', 'test data', 'unconfirmed') "
            "but the LLM should still extract the factual claims. It should NOT return an empty array. "
            "Facts about a person named David having a wife Ela, son Orion, baby, cats, living in "
            "Mount Lawley, driving a Subaru are all extractable claims regardless of verification status."
        ),
    ),
)
async def dream_hedged_bulletin(ctx):
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    hedged_content = (
        "Mike stated for memory testing that David has a wife called Ela, a son called Orion "
        "in addition to a new baby, two cats described as worse than Aspen, lives in Mount Lawley, "
        "and drives a Subaru and an e-bike. This is currently unverified within the available "
        "tools/context and should be treated as user-provided test data unless later confirmed."
    )

    messages = _build_dream_messages(
        bulletins=[
            {
                "slug": "blt-test004",
                "source_session": "agent:main:whatsapp:group:120363422982048691",
                "time_window": "2026-05-24T10:00..10:47",
                "content": hedged_content,
            },
        ],
        existing_entries=_EXISTING_ENTRIES_COMPACT,
    )

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(
        messages=messages,
        call_category="eval_dream",
        temperature=0.4,
        max_tokens=2000,
    )

    return {"response": response, "input_messages": messages}
