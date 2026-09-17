"""Assistant-row WhatsApp message-id persistence (reaction enabler).

Bob's own sends never recorded their WhatsApp message ids, so nothing could
reference them later (reaction targeting, reply-to). The send tool now
tracks each delivered effect on the spec's ``send_records``;
``_record_history`` stamps the ids onto the assistant row's metadata.sends,
and a ``send_message_result`` frame arriving after the fact back-fills the
row by PK. Pinned here at the three seams:

- service level: _track_send/_record_send_wa_id ordering races;
- runner level: _record_history metadata shape + row-id stamping;
- repo level: message_by_wa_id / stamp_send_wa_id round-trip.
"""

from __future__ import annotations

import json
from typing import Any

from server.repositories.history import HistoryRepository
from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

SESSION = "agent:main:whatsapp:dm:614000000001"
WA_ID = "3EB0F117B8C4A1B2C3D4"


def _svc(db) -> WhatsAppBridgeService:
    svc = object.__new__(WhatsAppBridgeService)
    svc._ws = None
    svc.db = db
    svc._send_reg = {}
    svc._wa_results = {}
    return svc


class StubSessionService:
    """Captures _record_history's add_message call; returns a fixed row id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def add_message(self, session_key, role, content, **kwargs) -> str:
        self.calls.append({"session_key": session_key, "role": role,
                           "content": content, **kwargs})
        return "row-1"


# --- Service level: the two result-arrival races ---

async def test_result_before_track_stashes_then_consumed(db):
    svc = _svc(db)
    await svc._record_send_wa_id("req-1", WA_ID)
    assert svc._wa_results == {"req-1": WA_ID}

    records: list[dict] = []
    svc._track_send(records, "req-1", "hello")
    assert records[0]["wa_message_id"] == WA_ID
    assert svc._wa_results == {}
    assert svc._send_reg["req-1"] is records[0]


async def test_result_between_track_and_record_mutates_entry(db):
    svc = _svc(db)
    records: list[dict] = []
    svc._track_send(records, "req-1", "hello")
    await svc._record_send_wa_id("req-1", WA_ID)
    assert records[0]["wa_message_id"] == WA_ID
    # Entry stays registered until the row records (message_id stamped).
    assert svc._send_reg["req-1"]["message_id"] is None


async def test_result_after_record_stamps_row_by_pk(db):
    svc = _svc(db)
    from server.services.session_service import SessionService

    row_id = await SessionService.from_db(db).add_message(
        SESSION, "assistant", "hello", metadata={"sends": [
            {"request_id": "req-1", "wa_message_id": None}]})
    svc._track_send([], "req-1", "hello")
    svc._send_reg["req-1"]["message_id"] = row_id
    await svc._record_send_wa_id("req-1", WA_ID)

    row = await HistoryRepository(db).message_by_wa_id(SESSION, WA_ID)
    assert row is not None and row["id"] == row_id
    sends = json.loads(row["metadata"])["sends"]
    assert sends[0]["wa_message_id"] == WA_ID
    # Stamped entries leave the registry.
    assert "req-1" not in svc._send_reg


async def test_unknown_request_and_failures_never_raise(db):
    svc = _svc(db)
    await svc._record_send_wa_id("never-tracked", WA_ID)  # stashes, no raise
    # Garbage metadata on the row must not explode the stamp path.
    from server.services.session_service import SessionService

    row_id = await SessionService.from_db(db).add_message(
        SESSION, "assistant", "x", metadata={"sends": "not-a-list"})
    entry = {"request_id": "req-2", "text": "x", "wa_message_id": None,
             "message_id": row_id}
    svc._send_reg["req-2"] = entry
    await svc._record_send_wa_id("req-2", WA_ID)  # returns quietly


async def test_registry_trim_bounds_memory(db):
    svc = _svc(db)
    for i in range(svc._SEND_REG_MAX + 10):
        svc._track_send([], f"req-{i}", "x")
    assert len(svc._send_reg) == svc._SEND_REG_MAX
    assert "req-0" not in svc._send_reg


# --- Runner level: _record_history metadata + stamping ---

async def test_record_history_writes_sends_metadata_and_stamps_entries(ctx):
    from server.services.dispatch_runner import DispatchRunner, DispatchSpec

    spec = DispatchSpec(
        session_key=SESSION, system_content="", tools=[],
        call_category="whatsapp_incoming", send_tool_name="send",
        dispatch_id="d-1", history_policy="delivered_only",
        message_was_sent=[True], sent_texts=["hello"],
        send_records=[{"request_id": "req-1", "text": "hello",
                       "wa_message_id": WA_ID, "message_id": None}])
    stub = StubSessionService()
    await DispatchRunner(ctx)._record_history(spec, stub, "hello")

    assert len(stub.calls) == 1
    meta = stub.calls[0]["metadata"]
    assert meta == {"sends": [{"request_id": "req-1", "wa_message_id": WA_ID}]}
    assert spec.send_records[0]["message_id"] == "row-1"


async def test_record_history_no_sends_no_metadata(ctx):
    from server.services.dispatch_runner import DispatchRunner, DispatchSpec

    spec = DispatchSpec(
        session_key=SESSION, system_content="", tools=[],
        call_category="whatsapp_incoming", send_tool_name="send",
        dispatch_id="d-1", history_policy="delivered_only",
        message_was_sent=[True], sent_texts=["hello"])
    stub = StubSessionService()
    await DispatchRunner(ctx)._record_history(spec, stub, "hello")
    assert stub.calls[0]["metadata"] is None


async def test_record_history_nothing_sent_records_nothing(ctx):
    from server.services.dispatch_runner import DispatchRunner, DispatchSpec

    spec = DispatchSpec(
        session_key=SESSION, system_content="", tools=[],
        call_category="whatsapp_incoming", send_tool_name="send",
        dispatch_id="d-1", history_policy="delivered_only",
        message_was_sent=[False], sent_texts=[],
        send_records=[{"request_id": "req-1", "text": "x",
                       "wa_message_id": None, "message_id": None}])
    stub = StubSessionService()
    await DispatchRunner(ctx)._record_history(spec, stub, "")
    assert stub.calls == []
    assert spec.send_records[0]["message_id"] is None


# --- Repo level ---

async def test_message_by_wa_id_finds_user_and_assistant_rows(db):
    from server.services.session_service import SessionService

    session_svc = SessionService.from_db(db)
    await session_svc.add_message(
        SESSION, "user", "see you at 6",
        metadata={"wa_message_id": "WA-USER-1"})
    await session_svc.add_message(
        SESSION, "assistant", "sounds good",
        metadata={"sends": [{"request_id": "r1", "wa_message_id": "WA-BOB-1"}]})

    repo = HistoryRepository(db)
    user_row = await repo.message_by_wa_id(SESSION, "WA-USER-1")
    bob_row = await repo.message_by_wa_id(SESSION, "WA-BOB-1")
    assert user_row is not None and user_row["role"] == "user"
    assert bob_row is not None and bob_row["role"] == "assistant"
    assert await repo.message_by_wa_id(SESSION, "WA-MISSING") is None


async def test_message_by_wa_id_scoped_to_conversation(db):
    from server.services.session_service import SessionService

    await SessionService.from_db(db).add_message(
        "agent:main:whatsapp:dm:614000000002", "user", "other chat",
        metadata={"wa_message_id": "WA-OTHER"})
    assert await HistoryRepository(db).message_by_wa_id(SESSION, "WA-OTHER") is None


async def test_stamp_send_wa_id_idempotent(db):
    from server.services.session_service import SessionService

    row_id = await SessionService.from_db(db).add_message(
        SESSION, "assistant", "hi", metadata={"sends": [
            {"request_id": "r1", "wa_message_id": None}]})
    repo = HistoryRepository(db)
    await repo.stamp_send_wa_id(row_id, "r1", "WA-1")
    await repo.stamp_send_wa_id(row_id, "r1", "WA-1")
    row = await repo.message_by_wa_id(SESSION, "WA-1")
    sends = json.loads(row["metadata"])["sends"]
    assert len(sends) == 1 and sends[0]["wa_message_id"] == "WA-1"


async def test_message_by_wa_id_ignores_rows_that_merely_reference(db):
    """Prod regression (2026-09-14): a removal reaction resolved its target
    to the ADD reaction row — its reaction.target_wa_message_id matched the
    metadata-LIKE scan and it was newer than the real target. Rows that
    merely reference an id (reaction targets, quotes) must not win."""
    from server.services.session_service import SessionService

    session_svc = SessionService.from_db(db)
    await session_svc.add_message(
        SESSION, "assistant", "the real target", dispatched=1,
        metadata={"sends": [{"request_id": "r1", "wa_message_id": "WA-T"}]})
    await session_svc.add_message(
        SESSION, "user", '[reaction ❤️ to your message: "the real target"]',
        dispatched=1, provenance="wa_reaction",
        metadata={"wa_message_id": "WA-R",
                  "reaction": {"emoji": "❤️", "target_wa_message_id": "WA-T"}})

    row = await HistoryRepository(db).message_by_wa_id(SESSION, "WA-T")
    assert row is not None
    assert row["content"] == "the real target"
    assert row["provenance"] != "wa_reaction"
