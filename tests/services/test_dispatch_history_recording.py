"""Per-round history recording on re-flown specs (2026-09-20 duplicate rows).

The attention coordinator re-arms a dispatch's stored spec closure for
messages that arrive mid-turn, so one DispatchSpec can run several rounds —
its sent_texts/send_records accumulate across them. _record_history used to
join ALL accumulated texts into a fresh assistant row at the end of every
round: a 5-round dispatch wrote 5 rows, the later ones re-containing the
earlier rounds' text (live: the Thomas merch DM showed 7 rows for 4 real
deliveries, incl. verbatim duplicates from NO_REPLY rounds that sent
nothing).

Pinned here, at the _record_history seam (plus one end-to-end run()):
- a round records only the sent_texts IT added (recorded_texts cursor)
- a round that added nothing (NO_REPLY / tool-only re-flight) writes no row
- send_records stamp to their own round's row; each row's metadata.sends
  carries only its own sends
- re-flight after detach inherits sent_texts for the duplicate-send guard
  but must not re-record them (cursor synced at the inheritance site)
"""

from __future__ import annotations

import json

import pytest

from server.services.dispatch_runner import DispatchRunner, DispatchSpec
from server.services.session_service import SessionService


@pytest.fixture
def stub_llm(monkeypatch):
    """chat_with_tools returns canned text (no tool calls); swap the reply
    via ``stub_llm["reply"] = …``. Mirrors test_dispatch_rescue_gating."""
    from server.services.llm_dispatch import LLMDispatchService
    seen: dict = {"reply": "stub reply"}

    async def _chat_with_tools(self, messages, tools, **kwargs):
        seen["messages"] = messages
        return seen["reply"]

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat_with_tools)
    return seen


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "hello"}]

    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _build)


def _spec(session_key: str, policy: str = "delivered_only") -> DispatchSpec:
    return DispatchSpec(
        session_key=session_key,
        system_content="system",
        tools=[],
        call_category="whatsapp_incoming",
        send_tool_name="send_whatsapp_message",
        dispatch_id="test-dispatch",
        history_policy=policy,
        message_was_sent=[False],
        sent_texts=[],
        send_records=[],
    )


def _send_entry(request_id: str) -> dict:
    return {"request_id": request_id, "text": "x",
            "wa_message_id": None, "message_id": None}


async def _rows(db, session_key: str) -> list:
    # rowid, not created_at: rounds land within the same second and the
    # tie order is unspecified.
    return await db.fetch_all(
        "SELECT id, content, metadata FROM messages "
        "WHERE conversation_id = ? AND role = 'assistant' ORDER BY rowid",
        (session_key,))


async def test_reflight_records_only_new_texts(ctx, db):
    key = "test:rec:reflight"
    spec = _spec(key)
    spec.message_was_sent[0] = True
    runner = DispatchRunner(ctx)

    spec.sent_texts.append("first reply")
    spec.send_records.append(_send_entry("req-1"))
    await runner._record_history(spec, SessionService(ctx), "first reply")

    # Round 2 (re-flight) sends one more message.
    spec.sent_texts.append("second reply")
    spec.send_records.append(_send_entry("req-2"))
    await runner._record_history(spec, SessionService(ctx), "second reply")

    rows = await _rows(db, key)
    assert [r["content"] for r in rows] == ["first reply", "second reply"], \
        "each row must carry only its own round's send, not the accumulated text"


async def test_noop_round_writes_no_row(ctx, db):
    """A re-flight that sent nothing new (NO_REPLY or tool-only round) must
    not re-write prior texts as a fresh duplicate row."""
    key = "test:rec:noop"
    spec = _spec(key)
    spec.message_was_sent[0] = True
    runner = DispatchRunner(ctx)

    spec.sent_texts.append("the one real reply")
    spec.send_records.append(_send_entry("req-1"))
    await runner._record_history(spec, SessionService(ctx), "the one real reply")
    # Round 2 ends NO_REPLY: message_was_sent stays True (set on round 1),
    # sent_texts unchanged — the exact 01:10:36 duplicate shape.
    await runner._record_history(spec, SessionService(ctx), "NO_REPLY")
    await runner._record_history(spec, SessionService(ctx), "")

    rows = await _rows(db, key)
    assert len(rows) == 1, "nothing-new rounds must not add rows"


async def test_send_records_stamp_to_their_own_row(ctx, db):
    key = "test:rec:stamps"
    spec = _spec(key)
    spec.message_was_sent[0] = True
    runner = DispatchRunner(ctx)

    spec.sent_texts += ["one", "two"]
    spec.send_records += [_send_entry("req-1"), _send_entry("req-2")]
    await runner._record_history(spec, SessionService(ctx), "one\n\ntwo")

    spec.sent_texts.append("three")
    spec.send_records.append(_send_entry("req-3"))
    await runner._record_history(spec, SessionService(ctx), "three")

    rows = await _rows(db, key)
    assert [r["content"] for r in rows] == ["one\n\ntwo", "three"]
    # Row 1 keeps its own sends; row 2 only req-3 — and req-1/req-2 keep
    # pointing at row 1 so late wa-id frames back-fill the right row.
    sends1 = json.loads(rows[0]["metadata"])["sends"]
    sends2 = json.loads(rows[1]["metadata"])["sends"]
    assert [s["request_id"] for s in sends1] == ["req-1", "req-2"]
    assert [s["request_id"] for s in sends2] == ["req-3"]
    stamps = {r["request_id"]: r["message_id"] for r in spec.send_records}
    assert stamps["req-1"] == rows[0]["id"] == stamps["req-2"]
    assert stamps["req-3"] == rows[1]["id"], \
        "round-2 stamping must not re-point round-1 send records"


async def test_detached_reflight_inheritance_not_rerecorded(ctx, db):
    """The re-flight site inherits sent_texts for the duplicate-send guard
    and syncs recorded_texts — the inherited texts already have rows."""
    key = "test:rec:inherit"
    spec = _spec(key)
    spec.message_was_sent[0] = True
    spec.sent_texts.append("sent before detach")
    spec.recorded_texts = len(spec.sent_texts)  # synced at the reflight site
    spec.sent_texts.append("sent by the live re-flight")
    spec.send_records.append(_send_entry("req-2"))

    await DispatchRunner(ctx)._record_history(
        spec, SessionService(ctx), "sent by the live re-flight")

    rows = await _rows(db, key)
    assert [r["content"] for r in rows] == ["sent by the live re-flight"], \
        "inherited (already-recorded) texts must not be re-joined"


async def test_merged_policy_records_only_new_texts(ctx, db):
    key = "test:rec:merged"
    spec = _spec(key, policy="merged_skip_no_reply")
    spec.message_was_sent[0] = True
    runner = DispatchRunner(ctx)

    spec.sent_texts.append("group reply one")
    await runner._record_history(spec, SessionService(ctx), "narration one")

    spec.sent_texts.append("group reply two")
    await runner._record_history(spec, SessionService(ctx), "narration two")

    rows = await _rows(db, key)
    assert [r["content"] for r in rows] == [
        "narration one\n\ngroup reply one",
        "narration two\n\ngroup reply two",
    ], "merged rows must join the round's own result + sends only"


async def test_run_reflight_end_to_end(ctx, db, stub_llm, stub_history):
    """Two coordinator re-flights of one spec (the live Thomas-DM shape):
    round 1 answers a real inbound via rescue, round 2 ends NO_REPLY after
    a task wake. Exactly one assistant row must exist."""
    class _FakeSendTool:
        def __init__(self, name: str):
            self.name = name
            self.spec: DispatchSpec | None = None
            self.delivered: list[str] = []

        async def handler(self, text: str) -> str:
            self.delivered.append(text)
            self.spec.message_was_sent[0] = True
            self.spec.sent_texts.append(text)
            return "sent"

    key = "test:rec:e2e"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "wheels look wrong", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    spec = _spec(key)
    spec.tools = [send_tool]
    send_tool.spec = spec
    stub_llm["reply"] = "On it — v2 with thinner wheels."

    await DispatchRunner(ctx).run(spec)
    rows = await _rows(db, key)
    assert [r["content"] for r in rows] == ["On it — v2 with thinner wheels."]

    # Re-flight: a task wake claims the turn; the model has nothing new to
    # say and answers NO_REPLY. The old bug re-wrote round 1's text here.
    await svc.add_message(key, "user", "## Task COMPLETED — image done",
                          dispatched=0, provenance="wake_nudge")
    stub_llm["reply"] = "NO_REPLY"
    await DispatchRunner(ctx).run(spec)

    rows = await _rows(db, key)
    assert [r["content"] for r in rows] == ["On it — v2 with thinner wheels."], \
        "NO_REPLY re-flight must not duplicate the recorded reply"
