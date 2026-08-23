"""Bob3 Phase I ingress tests: durable inbox appends at channel boundaries.

Each accepted external input must land in event_log atomically with its
legacy store write (invariants 1+2), deduplicated by (source, external_id).
Phase I is audit-only: dispatch still runs off the legacy stores, so these
tests only assert on event_log rows (and legacy double-store where it is
characterized behaviour, e.g. WhatsApp).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bob_server.heartbeat import EventLogReconciliationTask

from tests.services.test_email_inbound_characterization import (
    _make_service as _make_email_service,
    _message,
    _capture_create_task,
    _seed_inbox,
)
from tests.services.test_whatsapp_inbound_characterization import (
    TRUSTED_PHONE,
    _dm_payload,
    _make_service as _make_wa_service,
    _seed_contact,
    _stub_llm,
    _stub_workspace,
    immediate_patience,  # noqa: F401  (fixture re-export)
    stub_memory,  # noqa: F401
)


async def _events(db, source: str) -> list:
    return await db.fetch_all(
        "SELECT * FROM event_log WHERE source = ? ORDER BY id", (source,))


# ------------------------------------------------------------------ whatsapp


async def test_whatsapp_accepted_dm_appends_event_with_message_id_dedup_key(
        ctx, db, tmp_path, monkeypatch, immediate_patience, stub_memory):
    await _seed_contact(db, TRUSTED_PHONE)
    svc = _make_wa_service(ctx, tmp_path)
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return "NO_REPLY"
    _stub_llm(monkeypatch, behaviour)

    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "hi", "wamid-ing-1"))

    events = await _events(db, "whatsapp")
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "message.received"
    assert ev["external_id"] == "wamid-ing-1"
    stored = await db.fetch_one(
        "SELECT * FROM session_messages WHERE role='user' AND channel='whatsapp'")
    assert ev["binding_key"] == stored["session_key"]
    assert ev["conversation_id"] == stored["session_key"]


async def test_whatsapp_replay_dedups_event_log_but_still_double_stores_legacy(
        ctx, db, tmp_path, monkeypatch, immediate_patience, stub_memory):
    await _seed_contact(db, TRUSTED_PHONE)
    svc = _make_wa_service(ctx, tmp_path)
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return "NO_REPLY"
    _stub_llm(monkeypatch, behaviour)

    payload = _dm_payload(TRUSTED_PHONE, "hi again", "wamid-ing-dup")
    await svc._handle_incoming_message(payload)
    await svc._handle_incoming_message(payload)

    # Legacy store keeps its characterized no-dedup behaviour...
    users = await db.fetch_all(
        "SELECT * FROM session_messages WHERE role='user' AND channel='whatsapp'")
    assert len(users) == 2
    # ...but the durable inbox accepts once.
    assert len(await _events(db, "whatsapp")) == 1


# --------------------------------------------------------------------- email


async def test_email_ingress_appends_event_atomically_with_store(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_email_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    assert await svc.process_incoming_message(inbox, _message()) is True
    tasks[0].close()

    events = await _events(db, "email")
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "message.received"
    assert ev["external_id"] == "msg-1"
    stored = await db.fetch_one(
        "SELECT * FROM email_messages WHERE agentmail_message_id = ?", ("msg-1",))
    assert stored is not None
    thread = await db.fetch_one(
        "SELECT * FROM email_threads WHERE agentmail_thread_id = ?", ("thread-1",))
    assert ev["binding_key"] == thread["session_key"]


async def test_email_duplicate_message_id_appends_single_event(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_email_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    assert await svc.process_incoming_message(inbox, _message()) is True
    # Second delivery is rejected upstream by the email_messages UNIQUE check.
    assert await svc.process_incoming_message(inbox, _message()) is False
    for t in tasks:
        t.close()

    assert len(await _events(db, "email")) == 1


# --------------------------------------------------------------------- phone


async def test_phone_status_webhook_append_is_idempotent_per_sid_and_status(db):
    from bob_server.routers.phone import _append_call_status_event

    await db.execute(
        """INSERT INTO phone_calls (id, call_sid, phone_number, direction, status, started_at)
           VALUES (?, ?, ?, 'outbound', 'ringing', datetime('now'))""",
        (str(uuid.uuid4()), "CA-test-1", "+614000000099"),
    )

    await _append_call_status_event(db, "CA-test-1", "ringing", "")
    await _append_call_status_event(db, "CA-test-1", "ringing", "")  # Twilio retry
    await _append_call_status_event(db, "CA-test-1", "completed", "42")

    events = await _events(db, "phone")
    assert [e["external_id"] for e in events] == [
        "CA-test-1:ringing", "CA-test-1:completed"]
    assert events[0]["binding_key"] == "agent:main:phone:dm:614000000099"
    assert all(e["event_type"] == "call.status" for e in events)


async def test_phone_status_append_never_raises_for_unknown_call(db):
    from bob_server.routers.phone import _append_call_status_event

    await _append_call_status_event(db, "CA-missing", "completed", "")
    events = await _events(db, "phone")
    assert len(events) == 1
    assert events[0]["binding_key"] == "agent:main:phone:dm:unknown"


# ------------------------------------------------------------------- routine


async def test_routine_fired_event_is_idempotent_per_slot(ctx, db):
    from bob_server.services.routine_service import append_fired_event

    routine = {
        "id": "routine-1",
        "name": "Morning brief",
        "session_key": "agent:main:whatsapp:dm:614000000010",
    }
    await append_fired_event(ctx, routine, "2026-08-22T07:00:00+00:00")
    await append_fired_event(ctx, routine, "2026-08-22T07:00:00+00:00")  # replayed claim
    await append_fired_event(ctx, routine, "2026-08-23T07:00:00+00:00")

    events = await _events(db, "routine")
    assert [e["external_id"] for e in events] == [
        "routine-1:2026-08-22T07:00:00+00:00",
        "routine-1:2026-08-23T07:00:00+00:00",
    ]
    assert events[0]["event_type"] == "routine.fired"
    assert events[0]["binding_key"] == routine["session_key"]


# -------------------------------------------------------------- reconciliation


async def test_reconciliation_task_skips_sources_with_no_events_and_runs_clean(ctx, db):
    import bob_server.heartbeat as hb
    hb._last_event_log_reconcile = None

    # No events at all: baselines missing, task must no-op without error.
    await EventLogReconciliationTask().run(ctx)

    # With one whatsapp event + matching legacy row it must run the compare.
    from bob_server.repositories import Event, EventLogRepository
    await EventLogRepository(db).append(Event(
        event_type="message.received", binding_key="k", conversation_id="k",
        source="whatsapp", external_id="wamid-recon-1"))
    await db.execute(
        """INSERT INTO session_messages (id, session_key, role, content, channel)
           VALUES (?, 'k', 'user', 'hi', 'whatsapp')""",
        (str(uuid.uuid4()),))
    hb._last_event_log_reconcile = None
    await EventLogReconciliationTask().run(ctx)
