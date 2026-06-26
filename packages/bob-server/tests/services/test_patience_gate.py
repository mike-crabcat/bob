"""Tests for the patience gate: timing, relevance-gated skip, and pending semantics."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from unittest.mock import AsyncMock, patch

import pytest

from bob_server.services.patience_buffer import (
    PatienceBufferRegistry,
    PendingItem,
)
from bob_server.services.patience_gate import (
    PatienceDecision,
    _build_patience_context,
    _patience_system_prompt,
    submit_to_patience,
)


SESSION_KEY = "agent:main:test:group:test"


@pytest.fixture(autouse=True)
def reset_registry():
    """Isolate each test from buffer state left by other tests."""
    PatienceBufferRegistry.clear_all()
    yield
    PatienceBufferRegistry.clear_all()


def _msg(text: str, *, sender: str = "alice", ts: float | None = None) -> PendingItem:
    return PendingItem(
        item_type="message",
        timestamp=ts if ts is not None else monotonic(),
        sender_jid=f"{sender}@s.whatsapp.net",
        sender_name=sender,
        payload={"text": text},
    )


# ---------------------------------------------------------------------------
# Prompt variant selection
# ---------------------------------------------------------------------------


def test_prompt_timing_only_when_relevance_off():
    """When relevance_gating=False, prompt does NOT ask for a `respond` JSON field."""
    prompt = _patience_system_prompt("Bob", relevance_gating=False)
    # The legacy prompt uses the word "responding" in prose, but should not
    # include `respond` as a JSON output field. Check for the JSON template.
    assert '"respond"' not in prompt
    assert '"wait_seconds"' in prompt


def test_prompt_includes_respond_field_when_relevance_on():
    """When relevance_gating=True, prompt asks for `respond` alongside `wait_seconds`."""
    prompt = _patience_system_prompt("Bob", relevance_gating=True)
    assert '"respond"' in prompt
    assert '"wait_seconds"' in prompt
    # Mention-vs-address distinction must be stated — this was the root cause of
    # false negatives where kids' names in a question to Bob were treated as
    # addressees.
    assert "Names inside the message body are topics, not addressees" in prompt
    # Thread-continuation rule must be present so follow-ups aren't skipped.
    assert "follow-up to a thread" in prompt


# ---------------------------------------------------------------------------
# Skip path: respond=False skips dispatch entirely
# ---------------------------------------------------------------------------


async def test_submit_responds_false_skips_dispatch_and_marks_dispatched(ctx, db):
    """When the patience LLM returns respond=false, no dispatch and no timer."""
    dispatch_fn = AsyncMock()
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    buffer.add(_msg("casual chat"))

    decision = PatienceDecision(respond=False, wait_seconds=0.0, reason="not addressed")

    with patch(
        "bob_server.services.patience_gate._evaluate_urgency",
        new=AsyncMock(return_value=decision),
    ):
        await submit_to_patience(
            ctx, SESSION_KEY, _msg("more chat"), dispatch_fn,
            relevance_gating_enabled=True,
        )

    dispatch_fn.assert_not_awaited()
    assert buffer.timer_handle is None
    assert buffer.last_respond is False
    assert buffer.last_evaluated_at > 0


async def test_submit_responds_true_sets_timer(ctx):
    """When respond=true, the timer is set as before."""
    dispatch_fn = AsyncMock()
    decision = PatienceDecision(respond=True, wait_seconds=0.05, reason="addressed")

    with patch(
        "bob_server.services.patience_gate._evaluate_urgency",
        new=AsyncMock(return_value=decision),
    ):
        await submit_to_patience(
            ctx, SESSION_KEY, _msg("hey bob"), dispatch_fn,
            relevance_gating_enabled=True,
        )

    dispatch_fn.assert_not_awaited()  # not yet — timer hasn't fired
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    assert buffer.timer_handle is not None
    assert buffer.last_respond is True

    # Wait for the timer to fire
    await asyncio.sleep(0.15)
    dispatch_fn.assert_awaited_once()


async def test_relevance_disabled_never_skips_even_if_llm_returns_respond_false(ctx):
    """When relevance_gating_enabled=False, respond field is ignored and we always dispatch."""
    dispatch_fn = AsyncMock()
    decision = PatienceDecision(respond=False, wait_seconds=0.05, reason="ignored")

    with patch(
        "bob_server.services.patience_gate._evaluate_urgency",
        new=AsyncMock(return_value=decision),
    ):
        await submit_to_patience(
            ctx, SESSION_KEY, _msg("anything"), dispatch_fn,
            relevance_gating_enabled=False,
        )

    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    assert buffer.timer_handle is not None  # dispatch will fire
    assert buffer.last_respond is True  # the gating=False path overrides


# ---------------------------------------------------------------------------
# Pending semantics: skipped batches drop out of subsequent patience contexts
# ---------------------------------------------------------------------------


async def test_skipped_batch_excluded_from_next_patience_context(ctx, db):
    """After a skip, _build_patience_context only sees newly-arrived items."""
    # Seed a recent dispatched message so the context isn't empty
    await db.execute(
        "INSERT INTO session_messages (id, session_key, role, content, sender_id, dispatched, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, datetime('now'))",
        ("seed-1", SESSION_KEY, "user", "prior dispatched msg", "alice"),
    )

    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    base = monotonic()

    # First batch: two messages, then evaluated+skipped
    buffer.add(_msg("first msg", ts=base))
    buffer.add(_msg("second msg", ts=base + 0.01))
    buffer.last_evaluated_at = base + 0.02  # simulates skip having run

    # New message arrives after the skip
    buffer.add(_msg("newly arrived", ts=base + 1.0))

    context = await _build_patience_context(db, SESSION_KEY, buffer, max_context=10)

    assert "newly arrived" in context
    assert "first msg" not in context
    assert "second msg" not in context


async def test_fresh_buffer_includes_all_pending(db):
    """Without any prior evaluation, all pending items appear in context."""
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    base = monotonic()
    buffer.add(_msg("alpha", ts=base))
    buffer.add(_msg("beta", ts=base + 0.01))

    context = await _build_patience_context(db, SESSION_KEY, buffer, max_context=10)

    assert "alpha" in context
    assert "beta" in context


# ---------------------------------------------------------------------------
# Safety cap behavior
# ---------------------------------------------------------------------------


async def test_safety_cap_forces_dispatch_when_no_prior_decision(ctx):
    """Pre-evaluation cap hit forces dispatch (today's behavior)."""
    dispatch_fn = AsyncMock()

    # Pre-fill the buffer past the cap before submit_to_patience runs evaluation
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    for i in range(5):
        buffer.add(_msg(f"msg-{i}"))

    await submit_to_patience(
        ctx, SESSION_KEY, _msg("msg-5"),  # 6th message — cap is 5
        dispatch_fn,
        max_pending_items=5,
        relevance_gating_enabled=True,
    )

    dispatch_fn.assert_awaited_once()


async def test_safety_cap_flushes_without_dispatch_after_skip(ctx, db):
    """Cap hit after a skip decision flushes silently (no main LLM)."""
    # Seed session_messages so mark_dispatched has something to claim
    await db.execute(
        "INSERT INTO session_messages (id, session_key, role, content, sender_id, dispatched, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, datetime('now'))",
        ("m1", SESSION_KEY, "user", "buffered-1", "alice"),
    )
    await db.execute(
        "INSERT INTO session_messages (id, session_key, role, content, sender_id, dispatched, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, datetime('now'))",
        ("m2", SESSION_KEY, "user", "buffered-2", "alice"),
    )

    dispatch_fn = AsyncMock()
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    buffer.last_respond = False  # simulate prior skip decision

    # Add pending items past the cap
    for i in range(5):
        buffer.add(_msg(f"burst-{i}"))

    await submit_to_patience(
        ctx, SESSION_KEY, _msg("burst-5"),
        dispatch_fn,
        max_pending_items=5,
        relevance_gating_enabled=True,
    )

    dispatch_fn.assert_not_awaited()  # no main LLM fired

    # Messages should be marked dispatched (out of the pending queue)
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM session_messages WHERE session_key = ? AND dispatched = 1",
        (SESSION_KEY,),
    )
    assert row["c"] == 2


# ---------------------------------------------------------------------------
# Evaluate urgency: parse resilience
# ---------------------------------------------------------------------------


async def test_evaluate_urgency_returns_respond_true_on_context_build_failure(ctx):
    """If context building fails, fall back to respond=True (never skip on a fault)."""
    from bob_server.services.patience_gate import _evaluate_urgency

    buffer = PatienceBufferRegistry.get(SESSION_KEY)

    with patch(
        "bob_server.services.patience_gate._build_patience_context",
        side_effect=RuntimeError("boom"),
    ):
        decision = await _evaluate_urgency(
            ctx, SESSION_KEY, buffer, "Bob", "gpt-5.4-mini", 10,
            relevance_gating=True,
        )

    assert decision.respond is True
    assert decision.wait_seconds == 3.0


async def test_evaluate_urgency_parses_respond_field_when_relevance_on(ctx, db):
    """With relevance_gating=True, the `respond` field is honored from the parsed JSON."""
    from bob_server.services.patience_gate import _evaluate_urgency

    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    buffer.add(_msg("hello"))

    payload = json.dumps({"respond": False, "wait_seconds": 0, "reason": "casual"})

    async def _fake_chat(*args, **kwargs):
        return payload

    with patch("bob_server.services.llm_dispatch.LLMDispatchService") as MockSvc:
        MockSvc.return_value.chat = _fake_chat
        decision = await _evaluate_urgency(
            ctx, SESSION_KEY, buffer, "Bob", "gpt-5.4-mini", 10,
            relevance_gating=True,
        )

    assert decision.respond is False
    assert decision.reason == "casual"


# ---------------------------------------------------------------------------
# Phase 2: patience-off mode (always batch via fixed settle delay)
# ---------------------------------------------------------------------------


async def test_patience_off_skips_llm_evaluation(ctx):
    """When patience_enabled=False, _evaluate_urgency is never called."""
    dispatch_fn = AsyncMock()

    with patch(
        "bob_server.services.patience_gate._evaluate_urgency",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        await submit_to_patience(
            ctx, SESSION_KEY, _msg("anything"), dispatch_fn,
            patience_enabled=False,
            settle_seconds=0.05,
        )

    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    assert buffer.timer_handle is not None
    assert buffer.last_respond is True  # always respond when patience is off


async def test_patience_off_batches_burst_into_single_dispatch(ctx):
    """Three messages in quick succession produce ONE dispatch after settle_seconds."""
    dispatch_fn = AsyncMock()

    async def _eval_should_not_run(**kwargs):
        raise AssertionError("LLM eval must not run when patience is off")

    with patch("bob_server.services.patience_gate._evaluate_urgency", new=_eval_should_not_run):
        # Three messages in 10ms — well within the settle window.
        await submit_to_patience(ctx, SESSION_KEY, _msg("msg-1"), dispatch_fn,
                                 patience_enabled=False, settle_seconds=0.1)
        await asyncio.sleep(0.01)
        await submit_to_patience(ctx, SESSION_KEY, _msg("msg-2"), dispatch_fn,
                                 patience_enabled=False, settle_seconds=0.1)
        await asyncio.sleep(0.01)
        await submit_to_patience(ctx, SESSION_KEY, _msg("msg-3"), dispatch_fn,
                                 patience_enabled=False, settle_seconds=0.1)

    # Settle window hasn't elapsed yet — no dispatch yet.
    dispatch_fn.assert_not_awaited()

    # Wait for the settle window to elapse after the last message.
    await asyncio.sleep(0.15)
    dispatch_fn.assert_awaited_once()


async def test_patience_off_new_message_resets_settle_timer(ctx):
    """A message arriving during the settle window resets the timer."""
    dispatch_fn = AsyncMock()

    with patch("bob_server.services.patience_gate._evaluate_urgency",
               new=AsyncMock(side_effect=AssertionError("no LLM in patience-off"))):
        # First message at t=0 with a 0.1s settle window.
        await submit_to_patience(ctx, SESSION_KEY, _msg("first"), dispatch_fn,
                                 patience_enabled=False, settle_seconds=0.1)
        # Second message at t=0.05 — would have fired at t=0.1 without reset.
        await asyncio.sleep(0.05)
        await submit_to_patience(ctx, SESSION_KEY, _msg("second"), dispatch_fn,
                                 patience_enabled=False, settle_seconds=0.1)

        # At t=0.06 the original timer would have fired. Confirm it didn't.
        await asyncio.sleep(0.02)
        dispatch_fn.assert_not_awaited()

        # The reset timer fires at t=0.15 (0.05 + 0.1).
        await asyncio.sleep(0.10)
        dispatch_fn.assert_awaited_once()


async def test_patience_off_uses_configured_settle_seconds(ctx):
    """A larger settle_seconds delays the dispatch accordingly."""
    dispatch_fn = AsyncMock()

    with patch("bob_server.services.patience_gate._evaluate_urgency",
               new=AsyncMock(side_effect=AssertionError("no LLM in patience-off"))):
        await submit_to_patience(ctx, SESSION_KEY, _msg("hi"), dispatch_fn,
                                 patience_enabled=False, settle_seconds=0.3)

        # Halfway through the window — no dispatch yet.
        await asyncio.sleep(0.15)
        dispatch_fn.assert_not_awaited()

        # Past the window — dispatch fires.
        await asyncio.sleep(0.20)
        dispatch_fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# Session agenda injection
# ---------------------------------------------------------------------------


async def test_build_context_includes_agenda_when_provided(db):
    """When an agenda is passed, it appears in the patience context verbatim."""
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    buffer.add(_msg("hello"))

    agenda_text = "Family-assistant for Mike's Chamonix trip, July 2026."
    context = await _build_patience_context(
        db, SESSION_KEY, buffer, max_context=10, agenda=agenda_text,
    )

    assert "## Session context" in context
    assert agenda_text in context


async def test_build_context_omits_agenda_section_when_absent(db):
    """No agenda section when agenda is None or blank — preserves prior behavior."""
    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    buffer.add(_msg("hello"))

    context_none = await _build_patience_context(
        db, SESSION_KEY, buffer, max_context=10, agenda=None,
    )
    context_blank = await _build_patience_context(
        db, SESSION_KEY, buffer, max_context=10, agenda="   ",
    )

    assert "## Session context" not in context_none
    assert "## Session context" not in context_blank


async def test_evaluate_urgency_passes_stored_agenda_into_llm_call(ctx, db):
    """_evaluate_urgency fetches the stored agenda and includes it in the LLM context."""
    from bob_server.services.patience_gate import _evaluate_urgency

    buffer = PatienceBufferRegistry.get(SESSION_KEY)
    buffer.add(_msg("Audrey and Mabel like swimming — options?"))

    captured_messages: list = []

    async def _fake_chat(messages, *args, **kwargs):
        captured_messages.extend(messages)
        return json.dumps({"respond": True, "wait_seconds": 0, "reason": "test"})

    agenda_text = "Family-assistant for Mike's Chamonix trip."

    with patch("bob_server.services.session_agenda_service.SessionAgendaService") as MockAgenda:
        MockAgenda.return_value.get_agenda = AsyncMock(return_value=agenda_text)
        with patch("bob_server.services.llm_dispatch.LLMDispatchService") as MockSvc:
            MockSvc.return_value.chat = _fake_chat
            await _evaluate_urgency(
                ctx, SESSION_KEY, buffer, "Bob", "gpt-5.4-mini", 10,
                relevance_gating=True,
            )

    MockAgenda.return_value.get_agenda.assert_awaited_once_with(SESSION_KEY)
    user_content = next(m["content"] for m in captured_messages if m["role"] == "user")
    assert agenda_text in user_content
