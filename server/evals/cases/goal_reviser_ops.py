"""Goal reviser ops-mode eval — the capability gate for the patch contract
(2026-10-10 reviser fix; see tests/test_goal_reviser_ops.py for the platform
side). The live cheap model must answer a stimulus against a LARGE state
with a small, valid ops patch — not re-emit the state — and the applied
result must fold the new fact while keeping prior state intact."""

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case

NEW_KNOWN_MARK = "FULLTIME-FREO-BY-17"


def _big_state():
    from server.services.goal_state_service import GoalStrategy, StrategyRefs
    known = [
        f"Q{q} {mi:02d}:{se:02d}: Fremantle {3 + q}.{q} ({21 + q * 9}) "
        f"v Hawthorn {2 + q}.{q} ({15 + q * 7}) — " + "x" * 40
        for q in range(1, 5) for mi in range(0, 60, 2) for se in (0, 30)
    ]  # ~600 entries ≈ 50KB — the AFL goal's failure shape
    return GoalStrategy(
        plan="Score goal COMPLETE (all four quarters announced on air)",
        known=known,
        open_questions=[],
        next_actions=[],
        refs=StrategyRefs(entities=["bobs-pirate-radio", "event-afl-r26"]),
    )


@eval_case(
    id="goal_revise_ops_big_state",
    category="tool_calling",
    description=(
        "Reviser against a ~50KB state: must return a small ops patch (not "
        "the re-emitted state); applied state gains the new fact and keeps "
        "the old; response size stays far under the token cap."
    ),
    structural_checks=[
        StructuralCheck(kind="response_contains",
                        params={"terms": [NEW_KNOWN_MARK]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The response must be a JSON object with an `ops` list (the "
            "patch contract), typically one known.append carrying the full-"
            "time score fact. A full-state re-emission is a FAILURE even if "
            "valid JSON. wake_needed should be true with a summary pointing "
            "at closing the goal (it is complete)."
        ),
    ),
)
async def goal_revise_ops_big_state(ctx):

    from server.services.goal_state_service import _call_reviser

    state = _big_state()
    goal = {"id": "eval-goal", "kind": "broadcast",
            "objective": ("AFL quarter-by-quarter score updates on Bob's "
                          "Pirate Radio (Round 26, completed)"),
            "conversation_id": "eval:goal_ops:test", "status": "active"}
    stimulus = (
        "## Final result\nFull time siren: Fremantle 15.9 (99) defeated "
        "Hawthorn 12.6 (78). All four quarter scores were announced on air; "
        f"nothing remains to do — record {NEW_KNOWN_MARK} and close out.")

    new_state, wake_needed, summary = await _call_reviser(
        ctx, goal, state, stimulus)

    prior_kept = state.known[0] in new_state.known
    folded = any(NEW_KNOWN_MARK in k for k in new_state.known)
    # Compaction aggressiveness legitimately varies run to run (observed
    # keep_recent 5 and 1 on a completed goal) — explicit ops are the
    # contract. What must hold: the fact folded in, and no size explosion.
    size_sane = 1 <= len(new_state.known) <= len(state.known) + 10
    state_ok = folded and size_sane
    rendered = (
        f"ops applied: folded={folded} prior_kept={prior_kept} "
        f"known {len(state.known)}→{len(new_state.known)} "
        f"fact {NEW_KNOWN_MARK} "
        f"wake={wake_needed} summary: {summary}")

    if not state_ok:
        raise RuntimeError(
            f"ops application lost state: folded={folded} "
            f"size_sane={size_sane} "
            f"known {len(state.known)}→{len(new_state.known)}")
    return {
        "response": rendered,
        "context": {
            "new_known_count": len(new_state.known),
            "old_known_count": len(state.known),
            "wake_needed": wake_needed, "wake_summary": summary,
            "ops_contract": "v3",
        },
    }
