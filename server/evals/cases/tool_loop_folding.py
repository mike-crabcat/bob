"""Tool-loop folding eval — the capability gate for context folding
(server/services/tool_loop_folding.py, 2026-09-10).

A long tool loop whose early oversized result gets folded mid-loop must
still be able to cite numbers from that result's head AND tail excerpts,
plus a number from another aged (folded) result — the exact capability
question the change could regress. The case hard-fails if folding did not
actually engage (a silently-unfolded run would pass trivially).
"""

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case

# Fixed so the structural checks can assert them statically.
LEDGER_HEAD_NUMBER = "417729"
LEDGER_TAIL_WORD = "ZEBRALIGHT"
STEP3_NUMBER = "8302"


def _ledger_dump_text() -> str:
    head = (f"LEDGER SNAPSHOT\nopening balance code: {LEDGER_HEAD_NUMBER}\n"
            + "row: " + "x" * 90 + "\n")
    filler = "\n".join(f"entry {i:04d} " + "y" * 80 for i in range(80))
    tail = f"\nFINAL MARKER {LEDGER_TAIL_WORD}"
    return head + filler + tail  # ~8k chars: comfortably over fold threshold


def _step_text(n: int) -> str:
    # ~2.5k chars so steps are fold candidates too; the number rides the head.
    return (f"STEP {n} COMPLETE — code {STEP3_NUMBER if n == 3 else 1000 + n}\n"
            + "\n".join(f"tick {i:03d} " + "z" * 60 for i in range(35)))


@eval_case(
    id="tool_loop_folding_long_loop",
    category="tool_calling",
    description=(
        "6+ call tool loop with folding engaged: the final answer must still "
        "cite the head number, the tail marker, and an aged step number."
    ),
    structural_checks=[
        StructuralCheck(kind="response_contains",
                        params={"terms": [LEDGER_HEAD_NUMBER, STEP3_NUMBER,
                                          LEDGER_TAIL_WORD]}),
        StructuralCheck(kind="min_length", params={"min_length": 20}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The response must report exactly three values: the ledger head "
            f"number {LEDGER_HEAD_NUMBER}, step-3 code {STEP3_NUMBER}, and the "
            f"final marker {LEDGER_TAIL_WORD}. Numbers must match precisely — "
            "close-but-wrong digits are a failure (invention, not recall)."
        ),
    ),
)
async def tool_loop_folding_long_loop(ctx):
    from server.config import ToolLoopSettings
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tools import tool

    session_key = "eval:tool_loop_folding:test"

    # Deterministic folding within the eval regardless of deployment env:
    # tight thresholds, keep only the newest big result verbatim.
    saved = ctx.settings.tool_loop
    ctx.settings.tool_loop = ToolLoopSettings(
        folding_enabled=True, fold_keep_last=1, fold_size_threshold=2000,
        fold_head_chars=400, fold_tail_chars=200,
        history_view_keep=10, history_view_trigger_chars=2000)
    try:
        @tool
        async def read_ledger() -> str:
            """Return the full ledger snapshot (large)."""
            return _ledger_dump_text()

        @tool
        async def step(n: int) -> str:
            """Run processing step n (1-5) and return its log (large)."""
            return _step_text(n)

        messages = [
            {"role": "system", "content": (
                "You are a precise data assistant. Use tools exactly as "
                "instructed, then answer from what you saw.")},
            {"role": "user", "content": (
                "Call read_ledger once, then call step with n=1 through n=5 "
                "(five calls). Then reply in exactly this format and nothing "
                "else: HEAD=<code> STEP3=<code> TAIL=<word> — using the "
                "ledger's opening balance code, step 3's code, and the "
                "ledger's FINAL MARKER word.")},
        ]

        dispatch = LLMDispatchService(ctx)
        response = await dispatch.chat_with_tools(
            messages, [read_ledger, step],
            call_category="eval",
            session_key=session_key,
        )

        from server.services.tool_loop_folding import ELISION_TAG
        folded = sum(1 for m in messages
                     if m.get("type") == "function_call_output"
                     and isinstance(m.get("output"), str)
                     and ELISION_TAG in m["output"])
        if folded < 2:
            raise RuntimeError(
                "folding did not engage during the eval run "
                f"({folded} folded results) — the case cannot validate recall")
        return {
            "response": response,
            "context": {"folded_results": folded,
                        "transcript_items": len(messages)},
            "input_messages": messages,
        }
    finally:
        ctx.settings.tool_loop = saved
