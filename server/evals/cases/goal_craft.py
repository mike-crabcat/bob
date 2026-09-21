"""Goal-craft evals — the creation-moment quality bar (docs/goal-execution-
plan.md D8). The goal-craft skill teaches objective-writing; these cases
test that the model applies it without the skill body in context (the
pointer alone must route thinking well enough that a judge can tell).

Each case prompts with a vague human ask; the model drafts the goal
(objective + proof + first branches). Structural checks look for proof
language and branch shape; the judge scores scope and termination."""

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case


@eval_case(
    id="goal_craft_research_vague_ask",
    category="tool_calling",
    description=(
        "A fuzzy research ask must produce a goal whose objective names "
        "the artefact (written-down knowledge) and its proof (replayable "
        "evidence), plus at least two candidate strategies as branches."
    ),
    structural_checks=[
        StructuralCheck(kind="response_contains",
                        params={"terms": ["objective"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "FAIL the response if the drafted objective could be judged "
            "complete by a feeling ('understand', 'look into', 'investigate' "
            "without naming what gets written down) or if no proof/test of "
            "done is stated. PASS requires: a concrete artefact (spec, "
            "transcript, comparison), an evidence proof, and >=2 named "
            "candidate approaches to try as branches."
        ),
    ),
)
async def goal_craft_research_vague_ask(ctx):
    from server.services.llm_dispatch import LLMDispatchService
    messages = [
        {"role": "system", "content": (
            "You are Bob, an agent with a goal system. Draft the goal you "
            "would create. Reply with: OBJECTIVE (one paragraph), PROOF "
            "(what evidence shows done), BRANCHES (the 2-3 candidate "
            "approaches you would open as strategies).")},
        {"role": "user", "content": (
            "Mike says: 'the court website thing is doing my head in, can "
            "you see if there's any way to look up cases properly?'")},
    ]
    return await LLMDispatchService(ctx).chat_with_tools(
        messages, [], call_category="eval", session_key="eval:goal-craft")


@eval_case(
    id="goal_craft_negotiate_confirmation_shape",
    category="tool_calling",
    description=(
        "A negotiation ask must decompose into one confirmation-task per "
        "party with the channel conversation as completer and an explicit "
        "silence ladder — never a 'sort it out' goal."
    ),
    structural_checks=[
        StructuralCheck(kind="response_contains",
                        params={"terms": ["task"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "FAIL if confirmations are not explicit tasks tied to the "
            "conversation/channel that talks to each person, or if there "
            "is no fallback when someone doesn't reply (a follow-up "
            "ladder). PASS requires: per-party confirmation tasks with "
            "due times, written-confirmation evidence standard, and a "
            "ladder for silence."
        ),
    ),
)
async def goal_craft_negotiate_confirmation_shape(ctx):
    from server.services.llm_dispatch import LLMDispatchService
    messages = [
        {"role": "system", "content": (
            "You are Bob, an agent with a goal system and a task registry. "
            "Draft the goal you would create. Reply with: OBJECTIVE, "
            "CONFIRMATION TASKS (one per party: who confirms, via which "
            "conversation, by when), LADDER (what happens on silence).")},
        {"role": "user", "content": (
            "Mike says: 'can you sort out that dinner thing with Thomas — "
            "he was keen on the 27th if the venue works out'")},
    ]
    return await LLMDispatchService(ctx).chat_with_tools(
        messages, [], call_category="eval", session_key="eval:goal-craft")
