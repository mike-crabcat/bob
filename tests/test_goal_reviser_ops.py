"""Goal reviser ops-mode tests (2026-09-10 fix: the reviser returns a PATCH
of operations instead of re-emitting the whole state, so a grown state can
no longer truncate itself against the token cap; input side compacts above
30k chars; cap-truncated output fails fast instead of retrying identically
at temperature 0; achieved goals are pushed to CLOSE, not re-validated).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.config import Settings
from server.repositories.goals import GoalRepository
from server.services.goal_state_service import (
    GoalStrategy, _call_reviser, _state_view_for_prompt, apply_ops,
    strategy_json_for,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "server" / "schemas"


def _ops_json(ops: list[dict], *, wake: bool = False, summary: str = "") -> str:
    return json.dumps({"ops": ops, "wake_needed": wake,
                       "wake_summary": summary})


@pytest.fixture
async def db():
    from server.database import Database
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


@pytest.fixture
async def ctx(db, tmp_path):
    from server.context import AppContext
    return AppContext(db=db, settings=Settings(
        data_dir=tmp_path / "d", config_dir=tmp_path / "c",
        db_path=tmp_path / "d" / "bob.db"))


@pytest.fixture
def reviser(monkeypatch):
    """Mock the reviser LLM call; tests set `.response`; `.calls` records
    each call (messages + kwargs) so retry behavior is assertable."""
    from server.services.llm_dispatch import LLMDispatchService

    mock = AsyncMock()
    mock.response = _ops_json([{"op": "known.append", "values": ["x"]}])
    mock.calls: list[dict] = []

    async def _chat(self, messages, **kwargs):
        mock.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        return mock.response

    monkeypatch.setattr(LLMDispatchService, "chat", _chat)
    return mock


def _goal(objective="test goal") -> dict:
    return {"id": "g1", "kind": "broadcast", "objective": objective,
            "conversation_id": "conv", "status": "active"}


# ─── op application ───────────────────────────────────────────────────

def test_apply_ops_full_vocabulary():
    s = GoalStrategy(plan="old", known=["k1", "k2", "k3"],
                     open_questions=["q1"],
                     next_actions=[])
    n = apply_ops(s, [
        {"op": "known.append", "values": ["k4", "k1"]},   # dup skipped
        {"op": "known.remove", "prefixes": ["k1"]},
        {"op": "plan.set", "value": "new plan"},
        {"op": "open_questions.set", "values": []},
        {"op": "next_actions.set",
         "values": [{"action": "close the goal", "due": ""}]},
        {"op": "refs.add", "entities": ["e1", "e1"], "claims": ["c1"]},
        {"op": "known.keep_recent", "n": 5},
        {"op": "not-a-real-op"},                            # skipped
        {"op": "next_actions.set", "values": "not-a-list"},  # malformed skip
    ])
    assert n.known == ["k2", "k3", "k4"]
    assert n.plan == "new plan"
    assert n.open_questions == []
    assert [a.action for a in n.next_actions] == ["close the goal"]
    assert n.refs.entities == ["e1"] and n.refs.claims == ["c1"]
    assert s.known == ["k1", "k2", "k3"]  # original untouched


def test_apply_ops_keep_recent_and_remove():
    s = GoalStrategy(known=[f"fact {i}" for i in range(50)])
    n = apply_ops(s, [{"op": "known.keep_recent", "n": 10}])
    assert n.known == [f"fact {i}" for i in range(40, 50)]
    n2 = apply_ops(GoalStrategy(known=["alpha-1", "alpha-2", "beta-1"]),
                   [{"op": "known.remove", "prefixes": ["alpha-"]}])
    assert n2.known == ["beta-1"]  # prefix matches entry starts
    n3 = apply_ops(n, [{"op": "known.remove", "prefixes": ["fact 4"]}])
    assert n3.known == []  # every kept entry starts with "fact 4"


# ─── _call_reviser: ops contract, legacy fallback, compaction ─────────

async def test_ops_response_applies_patch(ctx, reviser):
    state = GoalStrategy(plan="p", known=["a"])
    reviser.response = _ops_json(
        [{"op": "known.append", "values": ["Q3: Freo 12.9 (81) v Haw 10.4 (64)"]}],
        wake=True, summary="quarter done")
    new_state, wake, summary = await _call_reviser(ctx, _goal(), state, "s")
    assert wake and summary == "quarter done"
    assert new_state.known == ["a", "Q3: Freo 12.9 (81) v Haw 10.4 (64)"]
    assert new_state.plan == "p"


async def test_legacy_full_state_still_accepted(ctx, reviser):
    state = GoalStrategy(plan="p", known=["a"])
    reviser.response = json.dumps({
        "state": {"v": 2, "plan": "legacy", "known": ["b"]},
        "wake_needed": False, "wake_summary": ""})
    new_state, wake, _ = await _call_reviser(ctx, _goal(), state, "s")
    assert new_state.plan == "legacy" and not wake


async def test_big_state_compacts_view_but_ops_apply_to_full(ctx, reviser):
    known = [f"Q{i}: score line {i} " + "x" * 60 for i in range(600)]
    state = GoalStrategy(plan="p", known=known)  # > 30k chars serialized
    view, compacted = _state_view_for_prompt(state)
    assert compacted and "compacted for this view" in view
    assert len(view) < len(strategy_json_for(state))
    assert len(known) == 600  # canonical state untouched

    reviser.response = _ops_json([{"op": "known.append", "values": ["final"]}])
    new_state, _, _ = await _call_reviser(ctx, _goal(), state, "s")
    assert new_state.known[0] == known[0]      # ops hit the FULL state
    assert new_state.known[-1] == "final"
    assert len(new_state.known) == 601
    assert "compacted for this view" in reviser.calls[0]["messages"][1]["content"]


async def test_legacy_state_refused_on_compacted_view(ctx, reviser):
    state = GoalStrategy(known=[f"k{i} " + "x" * 80 for i in range(500)])
    reviser.response = json.dumps({
        "state": {"v": 2, "known": ["only-this"]},
        "wake_needed": False, "wake_summary": ""})
    with pytest.raises(ValueError, match="compacted"):
        await _call_reviser(ctx, _goal(), state, "s")


async def test_empty_ops_is_valid_no_change(ctx, reviser):
    state = GoalStrategy(plan="p", known=["a"])
    reviser.response = _ops_json([])
    new_state, wake, _ = await _call_reviser(ctx, _goal(), state, "s")
    assert strategy_json_for(new_state) == strategy_json_for(state)
    assert not wake


# ─── truncation fast-fail ─────────────────────────────────────────────

async def test_cap_truncated_output_fails_fast_no_retry(ctx, reviser):
    state = GoalStrategy(plan="p")
    # ~36k chars ≥ 0.75×(16000×3): parse fails + size says cap-truncated.
    reviser.response = "{" + '"known": [' + "x" * 36_000
    with pytest.raises(ValueError, match="cap-truncated"):
        await _call_reviser(ctx, _goal(), state, "s")
    assert len(reviser.calls) == 1  # identical temp-0 retry skipped


async def test_small_invalid_output_still_retries_once(ctx, reviser):
    state = GoalStrategy(plan="p")
    reviser.response = "not json at all"
    with pytest.raises(ValueError, match="after retry"):
        await _call_reviser(ctx, _goal(), state, "s")
    assert len(reviser.calls) == 2


# ─── end-to-end through revise_goal_state ─────────────────────────────

async def test_revise_goal_state_ops_round_trip(ctx, db, reviser, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("server.services.wake_service.wake_conversation", wake)
    repo = GoalRepository(db)
    goal = await repo.create(
        conversation_id="conv", origin_conversation_id="conv",
        kind="broadcast", objective="AFL updates",
        strategy_json=strategy_json_for(GoalStrategy(plan="p", known=["a"])))
    goal_id = goal["id"]
    reviser.response = _ops_json(
        [{"op": "known.append", "values": ["full time: Freo by 17"]}],
        wake=False)
    from server.services.goal_state_service import revise_goal_state
    outcome = await revise_goal_state(ctx, goal_id, "FT siren",
                                      stimulus_id="t:ops")
    assert outcome["outcome"] == "revised" and outcome["wake"] == "no_wake"
    row = await repo.get(goal_id)
    assert "full time: Freo by 17" in row["strategy_json"]
    assert '"a"' in row["strategy_json"]  # prior state preserved by patch
