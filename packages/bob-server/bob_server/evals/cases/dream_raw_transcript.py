"""Raw-transcript extraction evals.

These test the claim-extraction LLM against bulletins in the new
raw-transcript format emitted by ``generate_session_bulletins``. They
complement the legacy ``dream.py`` cases, which test extraction against
synthetic markdown-style bulletins.

Each case mirrors the production call shape in
``claim_service.extract_claims_from_bulletin``: same system prompt builder,
same user-prompt wrapping, same dispatch parameters.
"""

from bob_server.evals.case import JudgeCriteria, StructuralCheck
from bob_server.evals.registry import eval_case


def _build_extraction_messages(
    *,
    bulletin_id: str,
    content: str,
    channel: str = "whatsapp:dm:test",
    visibility: str = "contact",
    known_entities: str = "",
) -> list[dict]:
    """Build the messages array the same way extract_claims_from_bulletin does."""
    from bob_server.services.memory.prompts import build_extraction_prompt
    from bob_server.services.memory.claim_types import build_extraction_prompt_section

    system_prompt = build_extraction_prompt(build_extraction_prompt_section(["person"]))

    bulletin_text = (
        f"[Bulletin: {bulletin_id}]\n"
        f"Channel: {channel}\n"
        f"Visibility: {visibility}\n\n"
        f"{content}"
    )
    user_prompt = f"## Bulletin\n\n{bulletin_text}"
    if known_entities:
        user_prompt += "\n\n## Known Entities\n\n" + known_entities

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


async def _run_extraction(ctx, messages) -> dict:
    from bob_server.services.llm_dispatch import LLMDispatchService

    dispatch = LLMDispatchService(ctx)
    response = await dispatch.chat(
        messages=messages,
        model=dispatch.memory_model,
        call_category="eval_dream_raw_transcript",
        temperature=0.2,
        max_tokens=4000,
    )
    return {"response": response, "input_messages": messages}


# ──────────────────────────────────────────────────────────────────────
# Case 1: prior-messages block must be skipped
# ──────────────────────────────────────────────────────────────────────

_CONTENT_WITH_PRIOR_FACT = """\
Prior messages (context only, do not extract):
[2026-06-13T09:00:00] [Mike contact-a1b2c3d4]: I'm relocating to Lisbon next month, got a flat in Alfama.

Window messages:
[2026-06-13T11:00:00] [David contact-cdda1eb1]: Hey, how's the weather over there?
[2026-06-13T11:00:30] [assistant]: Sunny and mild today."""


@eval_case(
    id="dream_raw_transcript_skips_prior",
    category="dream",
    description=(
        "Extraction must NOT produce claims from the 'Prior messages (context only, "
        "do not extract):' block. Facts appearing only in prior context (e.g. Mike "
        "relocating to Lisbon) must be ignored — they were extracted from a previous "
        "window."
    ),
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The window messages contain only small talk (weather question, 'sunny and mild') "
            "with no memory-worthy facts. The prior-messages block contains a concrete "
            "relocation fact about Mike moving to Lisbon.\n\n"
            "PASS if the response is an empty array `[]` OR contains only claims that can be "
            "derived from the window messages (which should be none).\n"
            "FAIL if any claim references Lisbon, Alfama, Mike relocating, or any fact that "
            "appeared only in the prior-messages block."
        ),
    ),
)
async def dream_raw_transcript_skips_prior(ctx):
    messages = _build_extraction_messages(
        bulletin_id="blt-eval-prior-001",
        content=_CONTENT_WITH_PRIOR_FACT,
    )
    return await _run_extraction(ctx, messages)


# ──────────────────────────────────────────────────────────────────────
# Case 2: [SYNTHETIC] assistant lines must be skipped
# ──────────────────────────────────────────────────────────────────────

_CONTENT_WITH_SYNTHETIC_FACT = """\
Window messages:
[2026-06-13T11:00:00] [Sylvain contact-e5f6a7b8]: Where do I live again?
[2026-06-13T11:00:30] [assistant][SYNTHETIC]: You live in Paris, near the Marais."""


@eval_case(
    id="dream_raw_transcript_skips_synthetic",
    category="dream",
    description=(
        "Extraction must NOT produce claims from assistant lines tagged [SYNTHETIC]. "
        "These are echoes of existing memory, not new ground truth."
    ),
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The window contains two messages: a user asking where they live, and an "
            "assistant response tagged [SYNTHETIC] stating 'You live in Paris, near the Marais'. "
            "The synthetic tag marks the assistant line as an echo of existing memory.\n\n"
            "PASS if the response is an empty array `[]` OR contains no claims about Sylvain "
            "living in Paris or the Marais.\n"
            "FAIL if any claim references Paris, the Marais, or Sylvain's residence — that "
            "fact appears only on the [SYNTHETIC] line and must be ignored."
        ),
    ),
)
async def dream_raw_transcript_skips_synthetic(ctx):
    messages = _build_extraction_messages(
        bulletin_id="blt-eval-synthetic-001",
        content=_CONTENT_WITH_SYNTHETIC_FACT,
    )
    return await _run_extraction(ctx, messages)


# ──────────────────────────────────────────────────────────────────────
# Case 3: plain window messages must still extract
# ──────────────────────────────────────────────────────────────────────

_CONTENT_WITH_WINDOW_FACT = """\
Window messages:
[2026-06-13T11:00:00] [Mike contact-a1b2c3d4]: Just booked the Seminyak villa for the Bali trip, $200/night for June 20-24."""


@eval_case(
    id="dream_raw_transcript_extracts_window",
    category="dream",
    description=(
        "Extraction must produce claims from non-synthetic window messages. "
        "Sanity check that the new format doesn't suppress legitimate extraction."
    ),
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
        StructuralCheck(
            kind="response_contains",
            params={"terms": ["seminyak", "bali"]},
            description="Response must reference Seminyak or Bali from the window message",
        ),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The window contains a single user message from Mike stating he booked a Seminyak "
            "villa for the Bali trip at $200/night for June 20-24.\n\n"
            "PASS if the response is a non-empty JSON array containing at least one claim "
            "referencing Seminyak, the Bali trip, the booking, or the June 20-24 dates.\n"
            "FAIL if the response is an empty array `[]` — there is clearly extractable "
            "factual content in the window."
        ),
    ),
)
async def dream_raw_transcript_extracts_window(ctx):
    messages = _build_extraction_messages(
        bulletin_id="blt-eval-window-001",
        content=_CONTENT_WITH_WINDOW_FACT,
    )
    return await _run_extraction(ctx, messages)


# ──────────────────────────────────────────────────────────────────────
# Case 4: [Name contact_id] form must resolve via Known Entities
# ──────────────────────────────────────────────────────────────────────

_CONTENT_WITH_CONTACT_ID = """\
Window messages:
[2026-06-13T11:00:00] [David Shedden contact-cdda1eb1]: My wife Ela just started teaching at Perth College."""

_KNOWN_ENTITIES_FOR_DAVID = (
    "- person-david-shedden (person) David Shedden\n"
    "- person-mike-cleaver (person) Mike Cleaver"
)


@eval_case(
    id="dream_raw_transcript_resolves_contact_id",
    category="dream",
    description=(
        "Extraction should produce claims from raw-transcript lines that use the "
        "[Name contact_id] bracket form, given a Known Entities section."
    ),
    structural_checks=[
        StructuralCheck(kind="json_valid", description="Response must be valid JSON"),
        StructuralCheck(
            kind="response_contains",
            params={"terms": ["person-david-shedden"]},
            description="Response must use the known entity slug person-david-shedden as a subject_id",
        ),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The window contains a single user message: 'My wife Ela just started teaching at "
            "Perth College.', attributed to David Shedden.\n\n"
            "PASS if the response is a non-empty JSON array that captures the factual content "
            "of the message: David's wife is Ela, and she teaches at Perth College. Minor "
            "variations in claim type naming (e.g. 'workplace' vs 'employer') are acceptable.\n"
            "FAIL only if the response is an empty array `[]` or if the extracted claims "
            "materially misrepresent the message (wrong relationship, wrong workplace)."
        ),
    ),
)
async def dream_raw_transcript_resolves_contact_id(ctx):
    messages = _build_extraction_messages(
        bulletin_id="blt-eval-contact-001",
        content=_CONTENT_WITH_CONTACT_ID,
        known_entities=_KNOWN_ENTITIES_FOR_DAVID,
    )
    return await _run_extraction(ctx, messages)
