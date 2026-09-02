"""Tests for voice_dispatch_service — the single owner of realtime voice dispatch."""

from __future__ import annotations

import json

from server.services.voice_dispatch_service import (
    build_inbound_instructions,
    build_outbound_instructions,
    extract_outcome,
    load_call_meta,
    mark_voice_subagent_complete,
    normalise_voice_modality,
)


# -- Modality normalisation (merged vocabulary, shared with outreach tools) --

def test_normalise_voice_modality_phone_aliases():
    # "voice" is the LLM's generic word for telephony — it must mean PHONE
    # (2026-08-14: mapping it to voice_link made Bob send links for explicit
    # phone-call requests and then claim the dialler was broken).
    for alias in ("phone", "call", "telephone", "dial", "twilio", "cell", "landline",
                  "sms", "voice", "voice_call", "voice call"):
        assert normalise_voice_modality(alias) == "phone", alias


def test_normalise_voice_modality_voice_link_aliases():
    for alias in ("voice_link", "voip", "realtime", "browser", "link",
                  "app", "web", "data", "whatsapp", "session"):
        assert normalise_voice_modality(alias) == "voice_link", alias


def test_normalise_voice_modality_unknown_and_empty():
    assert normalise_voice_modality("carrier_pigeon") is None
    assert normalise_voice_modality("") is None


# -- Instruction builders --

def test_build_outbound_instructions_contains_goal_and_protocol():
    instructions = build_outbound_instructions(contact_name="Alice", goal="confirm dinner at 7pm")
    assert "Alice" in instructions
    assert "confirm dinner at 7pm" in instructions
    assert "end_call" in instructions
    assert "PRIVATE NOTES, not a script" in instructions


def test_build_outbound_instructions_callee_speaks_first():
    instructions = build_outbound_instructions(goal="test")
    # Phone-call convention: the callee gets the first turn, not the agent.
    assert "let THEM speak first" in instructions


def test_build_outbound_instructions_no_proactive_disclosures():
    # "I'm an AI" and "on Mike's behalf" up front cloud the opening; Bob only
    # discloses either when the goal requires it or if asked.
    instructions = build_outbound_instructions(contact_name="Alice", goal="test")
    assert "identify yourself as Bob (an AI)" not in instructions
    assert "say you're calling on Mike's behalf" not in instructions
    assert "Do NOT introduce yourself by name" in instructions
    assert "do NOT say whose behalf you're calling on" in instructions
    assert "do NOT mention being an AI" in instructions
    assert "answer honestly and move on" in instructions


def test_build_outbound_instructions_short_turns():
    # Anti-monologue: one or two sentences per turn, one question at a time.
    instructions = build_outbound_instructions(goal="test")
    assert "ONE short sentence" in instructions
    assert "at most one" in instructions
    assert "one exchange at a time" in instructions


def test_build_outbound_instructions_hold_silence():
    # 2026-08-16 JB Hi-Fi call: the agent talked over hold music ("I'm waiting
    # for a person to answer..."). Hold music / IVR / announcements are not people.
    instructions = build_outbound_instructions(goal="test")
    assert "hold music" in instructions
    assert "say NOTHING" in instructions


def test_build_outbound_instructions_goal_is_notes_not_script():
    # 2026-08-16 JB Hi-Fi call: the goal contained a quotable greeting script
    # ("Introduce yourself naturally: 'Hi, I'm Bob, calling on behalf of Mike...'")
    # which the model recited verbatim, overriding the preamble. The goal must be
    # framed as private notes whose staging instructions are superseded.
    instructions = build_outbound_instructions(
        goal="Introduce yourself: 'Hi, I'm Bob, calling on behalf of Mike.' Ask about stock."
    )
    assert "Never recite, quote" in instructions
    assert "IGNORE any greeting" in instructions
    # The goal text itself still passes through (facts survive; framing handles manner).
    assert "Ask about stock" in instructions


def test_build_outbound_instructions_name_fidelity():
    # Connect noise gets mis-heard as a name; the agent must not invent one
    # (2026-08-15: opened a call to Ryan with "Hi Sophia" — hallucinated).
    instructions = build_outbound_instructions(contact_name="Ryan", goal="test")
    assert "ONLY the name given above" in instructions
    assert "Never invent" in instructions


def test_build_inbound_instructions_no_hangup_token():
    instructions = build_inbound_instructions("+61400111222", contact_name="Alice", agenda="wants a booking")
    assert "<hangup/>" not in instructions
    assert "end_call" in instructions
    assert "Alice" in instructions
    assert "wants a booking" in instructions


def test_build_inbound_instructions_without_agenda():
    instructions = build_inbound_instructions("+61400111222")
    assert "Context for this caller" not in instructions


# -- Outcome extraction --

def test_extract_outcome_none_when_no_outcome_tools():
    assert extract_outcome(None) is None
    assert extract_outcome([
        {"name": "get_caller_details", "arguments": {}, "output": "{}"},
        {"name": "end_call", "arguments": {}, "output": "{}"},
    ]) is None


def test_extract_outcome_success():
    calls = [{"name": "report_success", "arguments": {"summary": "Booked for 4", "details": "time: 7:30pm"}, "output": "{}"}]
    outcome = extract_outcome(calls)
    assert outcome == {"tool": "report_success", "summary": "Booked for 4", "details": "time: 7:30pm"}


def test_extract_outcome_last_report_wins():
    calls = [
        {"name": "report_failure", "arguments": {"reason": "no answer"}, "output": "{}"},
        {"name": "report_success", "arguments": {"summary": "got through later"}, "output": "{}"},
    ]
    assert extract_outcome(calls)["tool"] == "report_success"


# -- DB-backed helpers --

async def test_mark_voice_subagent_complete(ctx):
    await ctx.db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task, status, agent_type, created_at, updated_at)
           VALUES ('sub-1', 'agent:main:whatsapp:dm:1', 'subagent:x:1', 'call alice', 'running', 'openai_voice',
                   datetime('now'), datetime('now'))""",
    )
    await mark_voice_subagent_complete(ctx.db, "sub-1", "transcript text")
    row = await ctx.db.fetch_one("SELECT status, result FROM subagents WHERE id = 'sub-1'")
    assert row["status"] == "completed"
    assert row["result"] == "transcript text"


async def test_mark_voice_subagent_complete_missing_row_is_noop(ctx):
    # Must not raise — best-effort by contract.
    await mark_voice_subagent_complete(ctx.db, "no-such-subagent", "text")


async def test_load_call_meta_falls_back_to_db(ctx):
    meta = {"instructions": "say hi", "voice": "", "subagent_id": "sub-9"}
    await ctx.db.execute(
        """INSERT INTO phone_calls (id, call_sid, phone_number, direction, status, agenda,
                                    engine, realtime_meta, subagent_id, started_at)
           VALUES ('call-db-1', 'CA_db1', '+61400111222', 'outbound', 'ringing', 'test agenda',
                   'openai_realtime', ?, 'sub-9', datetime('now'))""",
        (json.dumps(meta),),
    )
    # No in-memory cache entry (simulates a restart between dial and answer).
    loaded = await load_call_meta(ctx.db, "CA_db1")
    assert loaded is not None
    assert loaded["engine"] == "openai_realtime"
    assert loaded["call_id"] == "call-db-1"
    assert loaded["phone_number"] == "+61400111222"
    assert loaded["realtime_meta"]["instructions"] == "say hi"
    assert loaded["realtime_meta"]["subagent_id"] == "sub-9"


async def test_load_call_meta_unknown_sid(ctx):
    assert await load_call_meta(ctx.db, "CA_nope") is None


async def test_dispatch_voice_link_targets_contact_dm_with_report_back(ctx):
    """voice_link from a group: memory attaches to the contact's DM session,
    the outcome reports back to the requesting session (ported from the
    retired reach_out_with_voice_call tool)."""
    from server.services.voice_dispatch_service import VoiceDispatchService

    await ctx.db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
           VALUES ('contact-1', 'Ryan', '+61423562040', 1, datetime('now'), datetime('now'))""",
    )
    await ctx.db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task, status, agent_type, created_at, updated_at)
           VALUES ('sub-vl', 'agent:main:whatsapp:group:123', 'subagent:g:1', 'call ryan',
                   'created', 'openai_voice', datetime('now'), datetime('now'))""",
    )

    result = await VoiceDispatchService(ctx).dispatch_contact_call(
        "sub-vl", "talk footy", "contact-1", "voice_link",
        "agent:main:whatsapp:group:123",
    )
    assert "voice_url" in result
    assert "next_step" in result

    row = await ctx.db.fetch_one(
        "SELECT origin_session_key, report_back_session_key, subagent_id FROM voice_sessions ORDER BY created_at DESC LIMIT 1"
    )
    assert row["origin_session_key"] == "agent:main:whatsapp:dm:61423562040"
    assert row["report_back_session_key"] == "agent:main:whatsapp:group:123"
    assert row["subagent_id"] == "sub-vl"


async def test_dispatch_voice_link_from_own_dm_no_report_back(ctx):
    """voice_link requested from the contact's own DM: origin is the DM, no
    redundant report_back target."""
    from server.services.voice_dispatch_service import VoiceDispatchService

    await ctx.db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
           VALUES ('contact-2', 'Blair', '+61401589328', 1, datetime('now'), datetime('now'))""",
    )
    await ctx.db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task, status, agent_type, created_at, updated_at)
           VALUES ('sub-vl2', 'agent:main:whatsapp:dm:61401589328', 'subagent:d:2', 'chat',
                   'created', 'openai_voice', datetime('now'), datetime('now'))""",
    )

    await VoiceDispatchService(ctx).dispatch_contact_call(
        "sub-vl2", "chat", "contact-2", "voice_link",
        "agent:main:whatsapp:dm:61401589328",
    )
    row = await ctx.db.fetch_one(
        "SELECT origin_session_key, report_back_session_key FROM voice_sessions ORDER BY created_at DESC LIMIT 1"
    )
    assert row["origin_session_key"] == "agent:main:whatsapp:dm:61401589328"
    assert row["report_back_session_key"] is None


async def test_voice_session_mirrors_to_phone_calls(ctx):
    """Browser voice-link calls must appear in the phone calls UI: create()
    writes a mirror phone_calls row and each lifecycle transition keeps it
    in sync (same id, direction='voice_link')."""
    from server.services.voice_session_service import VoiceSessionService

    svc = VoiceSessionService(ctx)
    created = await svc.create(
        "agent:main:whatsapp:group:123", goal="talk footy",
        phone_number="+61423562040",
    )
    token = created["id"]

    row = await ctx.db.fetch_one(
        "SELECT direction, status, agenda, engine, phone_number FROM phone_calls WHERE id = ?",
        (token,),
    )
    assert row is not None, "no phone_calls mirror row created"
    assert row["direction"] == "voice_link"
    assert row["status"] == "ringing"
    assert row["agenda"] == "talk footy"
    assert row["engine"] == "openai_realtime"
    assert row["phone_number"] == "+61423562040"

    # Link tapped: pending → active mirrors to the calls row.
    await svc.resolve(token)
    assert (await ctx.db.fetch_one(
        "SELECT status FROM phone_calls WHERE id = ?", (token,)))["status"] == "active"

    # Partial transcript mirrors too (live transcript in the calls UI).
    await svc.persist_transcript(token, "User: hi\nAgent: hello")
    assert (await ctx.db.fetch_one(
        "SELECT transcript FROM phone_calls WHERE id = ?", (token,)))["transcript"].startswith("User: hi")

    # Completion mirrors with outcome + duration.
    await svc.complete(
        token, "User: hi\nAgent: hello", 12.0,
        tool_calls=[{"name": "report_success", "arguments": {"summary": "all good"}, "output": "{}"}],
    )
    final = await ctx.db.fetch_one(
        "SELECT status, duration_seconds, outcome FROM phone_calls WHERE id = ?", (token,))
    assert final["status"] == "completed"
    assert final["duration_seconds"] == 12.0
    assert json.loads(final["outcome"])["summary"] == "all good"


def test_invalid_realtime_voice_falls_back(monkeypatch):
    """A TTS-only voice (fable) must never reach a realtime session — an
    invalid voice makes session.update fail and the call silently runs on
    default settings (2026-08-15 voice-link call)."""
    from server.config import Settings

    monkeypatch.setenv("BOB_OPENAI_REALTIME_VOICE", "fable")
    settings = Settings.from_env()
    assert settings.openai_realtime.voice == "cedar"

    monkeypatch.setenv("BOB_OPENAI_REALTIME_VOICE", "ash")
    settings = Settings.from_env()
    assert settings.openai_realtime.voice == "ash"


# -- Voice as a binding (Bob3 Phase VI item 5) --

async def test_dispatch_binds_call_to_person_conversation(ctx):
    """The call's subagent session key becomes a binding on the person's
    conversation, so transcripts/outcomes land on the person."""
    from server.repositories.conversations import ConversationRepository
    from server.services.voice_dispatch_service import VoiceDispatchService

    await ctx.db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
           VALUES ('contact-vb', 'Sarah', '+61411222333', 1, datetime('now'), datetime('now'))""",
    )
    await ctx.db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task, status, agent_type, created_at, updated_at)
           VALUES ('sub-vb', 'agent:main:whatsapp:group:g1', 'subagent:g:vb', 'call sarah',
                   'created', 'openai_voice', datetime('now'), datetime('now'))""",
    )
    await VoiceDispatchService(ctx).dispatch_contact_call(
        "sub-vb", "confirm dinner", "contact-vb", "voice_link",
        "agent:main:whatsapp:group:g1",
    )
    conv = await ConversationRepository(ctx.db).resolve("subagent:g:vb")
    assert conv is not None
    assert conv["id"] == "agent:main:whatsapp:dm:61411222333"
    binding = await ConversationRepository(ctx.db).get_binding("subagent:g:vb")
    assert binding["channel"] == "voice"
    assert binding["address"] == "+61411222333"


async def test_append_call_completed_event_lands_on_person(ctx):
    """call.completed events resolve through the call binding to the person's
    conversation; binding_key preserves the call endpoint."""
    from server.repositories.conversations import ConversationRepository
    from server.services.voice_dispatch_service import append_call_completed_event

    repo = ConversationRepository(ctx.db)
    person = await repo.ensure("agent:main:whatsapp:dm:61499000111")
    await repo.bind("subagent:x:call1", person["id"], channel="voice")

    await append_call_completed_event(
        ctx.db,
        external_id="call-abc",
        call_session_key="subagent:x:call1",
        origin_session_key="agent:main:whatsapp:group:origin",
        status="completed",
        outcome={"ok": True, "details": "dinner confirmed"},
        duration_seconds=42.0,
    )
    ev = await ctx.db.fetch_one(
        "SELECT * FROM event_log WHERE source='voice' AND external_id='call-abc'")
    assert ev is not None
    assert ev["event_type"] == "call.completed"
    assert ev["conversation_id"] == "agent:main:whatsapp:dm:61499000111"
    assert ev["binding_key"] == "subagent:x:call1"

    # Idempotent on (source, external_id).
    await append_call_completed_event(
        ctx.db, external_id="call-abc", call_session_key="subagent:x:call1",
        origin_session_key="", status="completed")
    rows = await ctx.db.fetch_all(
        "SELECT id FROM event_log WHERE source='voice' AND external_id='call-abc'")
    assert len(rows) == 1


async def test_append_call_completed_event_falls_back_to_origin(ctx):
    """Unbound call (e.g. legacy/inbound): the event lands on the origin
    conversation."""
    from server.services.voice_dispatch_service import append_call_completed_event

    await append_call_completed_event(
        ctx.db,
        external_id="call-unbound",
        call_session_key="",
        origin_session_key="agent:main:whatsapp:dm:61400000009",
        status="failed",
    )
    ev = await ctx.db.fetch_one(
        "SELECT conversation_id, binding_key FROM event_log WHERE external_id='call-unbound'")
    assert ev["conversation_id"] == "agent:main:whatsapp:dm:61400000009"
    assert ev["binding_key"] == "agent:main:whatsapp:dm:61400000009"


async def test_subagent_spawn_recorded_as_effect(ctx, monkeypatch):
    """claude/local spawns ride a durable subagent_spawn effect; the executor
    starts the run."""
    from server.services.subagent_service import SubagentService

    ran: list[str] = []

    async def _fake_run(self, subagent_id, task):
        ran.append(subagent_id)

    monkeypatch.setattr(SubagentService, "_run_subagent", _fake_run)

    result = await SubagentService(ctx).create_subagent(
        "write a haiku", "agent:main:whatsapp:dm:61400000001", agent_type="local",
    )
    assert result["ok"] is True
    effect = await ctx.db.fetch_one(
        "SELECT kind, status, payload_json FROM effects WHERE kind='subagent_spawn'")
    assert effect is not None
    assert effect["status"] == "delivered"
    import json as _json
    payload = _json.loads(effect["payload_json"])
    assert payload["executor"] == "local"
    assert payload["subagent_id"] == result["subagent_id"]
    # Let the spawned task run.
    import asyncio as _asyncio
    await _asyncio.sleep(0)
    assert ran == [result["subagent_id"]]
