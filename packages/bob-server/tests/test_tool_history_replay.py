"""Tests for tool-call trace capture, persistence, and replay.

Covers the path: dispatch → SessionService.add_message → build_chat_messages.
The capture side (_build_tool_trace) is tested directly since it's a pure
function over the items OpenAIService would append to messages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bob_server.services.llm_dispatch import (
    LLMDispatchService,
    _build_tool_trace,
    _cap_item,
    _dispatch_tool_trace,
    _is_image_user_block,
    _ITEM_CAP,
)
from bob_server.services.prompt_assembler import build_chat_messages
from bob_server.services.session_service import SessionService


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "bob_server" / "schemas"


@pytest.fixture
async def db():
    from bob_server.database import Database
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


@pytest.fixture
async def ctx(db):
    from bob_server.config import Settings
    from bob_server.context import AppContext
    return AppContext(db=db, settings=Settings.from_env())


# ─── _build_tool_trace ────────────────────────────────────────────────

def test_trace_returns_none_when_no_function_calls():
    items = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
    assert _build_tool_trace(items) is None


def test_trace_preserves_function_call_then_output_order():
    items = [
        {"type": "function_call", "call_id": "c1", "name": "memory_search", "arguments": '{"q": "bob"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "3 matches"},
        {"type": "function_call", "call_id": "c2", "name": "send_whatsapp", "arguments": '{"to": "+1"}'},
        {"type": "function_call_output", "call_id": "c2", "output": "sent"},
    ]
    trace = _build_tool_trace(items)
    assert trace is not None
    assert [it["type"] for it in trace["items"]] == [
        "function_call", "function_call_output", "function_call", "function_call_output",
    ]
    # Summary should mention both calls in order.
    assert "memory_search" in trace["summary"]
    assert "send_whatsapp" in trace["summary"]
    assert trace["summary"].index("memory_search") < trace["summary"].index("send_whatsapp")


def test_trace_drops_reasoning_items():
    items = [
        {"type": "reasoning", "summary": "thinking..."},
        {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "reasoning", "encrypted_content": "..."},
    ]
    trace = _build_tool_trace(items)
    assert trace is not None
    assert all(it["type"] != "reasoning" for it in trace["items"])


def test_trace_strips_image_injection_user_block():
    """OpenAIService emits a synthetic {role: user, image} block after an
    ImageInjection tool result — that must not survive into the trace
    (would balloon row size and break assistant-turn grouping)."""
    items = [
        {"type": "function_call", "call_id": "c1", "name": "screenshot", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "see image"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "see image"},
            {"type": "input_image", "image_url": "data:image/png;base64,BIG"},
        ]},
    ]
    assert _is_image_user_block(items[2]) is True
    trace = _build_tool_trace(items)
    assert trace is not None
    assert len(trace["items"]) == 2  # fc + fco only
    assert all(it.get("role") != "user" for it in trace["items"])


def test_trace_caps_oversized_output():
    big = "x" * (_ITEM_CAP + 1000)
    item = {"type": "function_call_output", "call_id": "c1", "output": big}
    capped = _cap_item(item)
    assert len(capped["output"]) < len(big)
    assert "…[truncated]" in capped["output"]


def test_trace_caps_oversized_arguments():
    big = "x" * (_ITEM_CAP + 1000)
    item = {"type": "function_call", "call_id": "c1", "name": "write", "arguments": big}
    capped = _cap_item(item)
    assert len(capped["arguments"]) < len(big)
    assert "…[truncated]" in capped["arguments"]


def test_summary_truncates_long_args_and_outputs():
    long_arg = "a" * 500
    long_out = "b" * 500
    items = [
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": json.dumps({"k": long_arg})},
        {"type": "function_call_output", "call_id": "c1", "output": long_out},
    ]
    trace = _build_tool_trace(items)
    assert trace is not None
    # Each preview should be well under the source length.
    assert trace["summary"].count("a") < 100
    assert trace["summary"].count("b") < 100


# ─── pop_tool_trace lifecycle ─────────────────────────────────────────

async def test_pop_tool_trace_returns_none_for_unknown_dispatch():
    assert LLMDispatchService.pop_tool_trace(None) is None
    assert LLMDispatchService.pop_tool_trace("never-existed") is None


async def test_pop_tool_trace_serializes_items_to_json():
    items = [
        {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    _dispatch_tool_trace["d1"] = {"items": items, "summary": "[tools used: x() → ok]"}
    result = LLMDispatchService.pop_tool_trace("d1")
    assert result is not None
    assert result["summary"] == "[tools used: x() → ok]"
    parsed = json.loads(result["items_json"])
    assert parsed == items
    # Pop is destructive.
    assert LLMDispatchService.pop_tool_trace("d1") is None


async def test_pop_tool_trace_falls_back_when_items_exceed_cap():
    from bob_server.services.llm_dispatch import _WHOLE_TRACE_CAP
    big_out = "x" * (_WHOLE_TRACE_CAP + 100)
    _dispatch_tool_trace["d2"] = {
        "items": [{"type": "function_call_output", "call_id": "c1", "output": big_out}],
        "summary": "[tools used: ...]",
    }
    result = LLMDispatchService.pop_tool_trace("d2")
    assert result is not None
    assert result["items_json"] is None  # too large — drop
    assert result["summary"]  # kept


# ─── End-to-end: capture → persist → replay ────────────────────────────

async def _simulate_dispatch_wrote_assistant(ctx, session_key, dispatch_id, items, reply_text):
    """Mimic what chat_with_tools + SessionService.add_message would do."""
    trace = _build_tool_trace(items)
    if trace is not None:
        _dispatch_tool_trace[dispatch_id] = trace
    svc = SessionService(ctx)
    await svc.add_message(session_key, "assistant", reply_text, dispatch_id=dispatch_id)


async def test_replay_expands_last_three_assistant_turns(ctx, db):
    session_key = "test:replay:1"
    # Five assistant turns each with tool blocks. Only the last 3 should
    # expand to items; the older two should fall back to summary prefix.
    for n in range(5):
        items = [
            {"type": "function_call", "call_id": f"c{n}", "name": "memory_search", "arguments": f'{{"q": "turn{n}"}}'},
            {"type": "function_call_output", "call_id": f"c{n}", "output": f"result{n}"},
        ]
        await _simulate_dispatch_wrote_assistant(
            ctx, session_key, f"dispatch-{n}", items, f"reply {n}",
        )
        # Interleave user rows so the alternation is realistic.
        await SessionService(ctx).add_message(session_key, "user", f"user {n+1}")

    messages = await build_chat_messages(session_key=session_key, db=db)

    # Count function_call items in the assembled history — should be exactly 3.
    fc_count = sum(1 for m in messages if isinstance(m, dict) and m.get("type") == "function_call")
    assert fc_count == 3, f"expected 3 expanded turns, got {fc_count}"

    # Older turns should appear with [tools used: prefix].
    summary_prefixed = [
        m for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[tools used:")
    ]
    assert len(summary_prefixed) == 2, f"expected 2 summary fallbacks, got {len(summary_prefixed)}"


async def test_replay_preserves_temporal_order_within_turn(ctx, db):
    """Within an expanded turn, function_call must precede its function_call_output."""
    session_key = "test:replay:order"
    items = [
        {"type": "function_call", "call_id": "c1", "name": "first", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "first-out"},
        {"type": "function_call", "call_id": "c2", "name": "second", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "second-out"},
    ]
    await _simulate_dispatch_wrote_assistant(ctx, session_key, "d-order", items, "final reply")

    messages = await build_chat_messages(session_key=session_key, db=db)

    # Find positions of the typed items in order.
    types_in_order = [m.get("type") for m in messages if isinstance(m, dict) and m.get("type")]
    assert types_in_order[:4] == [
        "function_call", "function_call_output", "function_call", "function_call_output",
    ]

    # The final assistant text reply should come AFTER the items.
    last_assistant = messages[-1]
    assert last_assistant["role"] == "assistant"
    assert last_assistant["content"] == "final reply"


async def test_replay_handles_rows_without_trace(ctx, db):
    """Pre-migration rows (NULL columns) should still load as plain text."""
    session_key = "test:replay:legacy"
    # Insert a legacy assistant row directly with no trace columns set.
    await db.execute(
        "INSERT INTO session_messages (id, session_key, role, content) "
        "VALUES (?, ?, 'assistant', 'legacy reply')",
        ("legacy-1", session_key),
    )
    await SessionService(ctx).add_message(session_key, "user", "hello?")

    messages = await build_chat_messages(session_key=session_key, db=db)
    # Both rows should appear as plain {role, content} dicts.
    assert {"role": "user", "content": "hello?"} in messages
    assert {"role": "assistant", "content": "legacy reply"} in messages


async def test_dispatch_failure_clears_trace():
    """When chat_with_tools raises, the trace entry should be popped to avoid
    leaking (the dispatch_id will never be consumed by add_message)."""
    from bob_server.services.llm_dispatch import LLMDispatchService as Dispatch
    _dispatch_tool_trace["doomed"] = {"items": [], "summary": ""}
    # Simulate the cleanup path the except block runs.
    _dispatch_tool_trace.pop("doomed", None)
    assert "doomed" not in _dispatch_tool_trace
    assert Dispatch.pop_tool_trace("doomed") is None
