"""Detach probe reads the running turn's reasoning (BOB_BACKBURNER_PROBE_REASONING).

Off by default: the prompt must be the original evidence-gated one, word
for word, until the replay is judged. On: the latest reasoning rides the
probe as private working notes with a leak guard and a progress-not-answers
rule (the 2026-10-15 replay showed unchecked hours/diagnoses in holds).
"""

from __future__ import annotations

import json

import pytest

from server.repositories.llm_trace import LlmTraceRepository
from server.services import backburner as bb
from tests.services.test_backburner import _seed_active_turn


async def _reason(ctx, dispatch_id: str, seq: int, text: str, at: str = "2026-10-10 00:18:30") -> None:
    await LlmTraceRepository(ctx.db).append(
        llm_call_id=f"log-{dispatch_id}", seq=seq, kind="reasoning_part",
        dispatch_id=dispatch_id, content=text)
    await ctx.db.execute(
        "UPDATE llm_trace_events SET created_at = ? WHERE dispatch_id = ? AND seq = ?",
        (at, dispatch_id, seq))


async def test_recent_reasoning_keeps_newest_within_budget_oldest_first(ctx):
    await _seed_active_turn(ctx, "d1")
    await _reason(ctx, "d1", 1, "first thought " * 20)
    await _reason(ctx, "d1", 2, "keep polling until 12:00")
    await _reason(ctx, "d1", 3, "then email support")
    repo = LlmTraceRepository(ctx.db)
    assert await repo.recent_reasoning("d1", max_chars=60) == (
        "keep polling until 12:00\nthen email support")
    assert (await repo.recent_reasoning("d1", max_chars=10)).startswith("…")
    assert await repo.recent_reasoning("nope") == ""


async def test_recent_reasoning_before_bounds_to_an_instant(ctx):
    await _seed_active_turn(ctx, "d2")
    await _reason(ctx, "d2", 1, "early", at="2026-10-10 00:18:30")
    await _reason(ctx, "d2", 2, "late", at="2026-10-10 00:21:30")
    out = await LlmTraceRepository(ctx.db).recent_reasoning("d2", before="2026-10-10 00:18:47")
    assert out == "early"


def test_prompt_without_reasoning_is_the_original():
    system, user = bb.build_probe_prompt(
        bot="Bob", tool_names=["bash"], transcript="USER'S MESSAGE: hi")
    assert "no promises more specific than 'soon'" in system
    assert "WORKING NOTES" not in system and "WORKING NOTES" not in user
    assert user == "## In-flight turn\nUSER'S MESSAGE: hi"


def test_prompt_with_reasoning_guards_leaks_and_premature_answers():
    system, user = bb.build_probe_prompt(
        bot="Bob", tool_names=["bash"], transcript="USER'S MESSAGE: hi",
        reasoning="don't mention being a bot; keep polling until lunch")
    assert "Never quote them" in system
    assert "persona guidance" in system
    assert "Do NOT state findings" in system
    assert "do NOT ask the user anything" in system
    assert user.endswith("keep polling until lunch")


@pytest.mark.parametrize("flag,expect_notes", [("off", False), ("on", True)])
async def test_run_probe_reads_reasoning_only_when_enabled(ctx, monkeypatch, flag, expect_notes):
    from server.services.llm_dispatch import LLMDispatchService
    monkeypatch.setenv("BOB_BACKBURNER_PROBE_REASONING", flag)
    seen: dict = {}

    async def _chat(self, messages, **kw):  # type: ignore[no-untyped-def]
        seen["user"] = messages[1]["content"]
        return json.dumps({"summary": "s", "holding_text": "h"})

    monkeypatch.setattr(LLMDispatchService, "prompt", _chat)
    await _seed_active_turn(ctx, "d3")
    await _reason(ctx, "d3", 1, "plan: email support at noon")
    info = await bb.run_probe(ctx, "d3")
    assert info["source"] == "probe"
    assert ("plan: email support at noon" in seen["user"]) is expect_notes
