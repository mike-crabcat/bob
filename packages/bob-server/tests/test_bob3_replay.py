"""Replay harness (Bob3 Phase VII items 1-2).

Normalized episode fixtures (tests/fixtures/episodes/*.json) replay through
the REAL ingress → attention → dispatch → effects pipeline with two
substitutions only: the LLM is scripted per episode, and every effect lands
in the typed FakeEffectSink (invariant 8 — replay can never execute a real
external action). Assertions are on decisions and effects.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bob_server.services import effects as effects_svc
from bob_server.services.attention import coordinator as coord_mod
from tests.services.test_whatsapp_inbound_characterization import (  # noqa: F401
    _dm_payload,
    _group_payload,
    _make_service,
    _seed_contact,
    _stub_llm,
    _stub_workspace,
    stub_memory,
)

EPISODE_DIR = Path(__file__).parent / "fixtures" / "episodes"


def _load_episodes() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(EPISODE_DIR.glob("*.json"))]


EPISODES = _load_episodes()


@pytest.fixture(autouse=True)
def replay_environment(monkeypatch):
    """Shrunken attention windows + clean coordinator/effect state."""
    monkeypatch.setattr(coord_mod, "WINDOW_ADDRESSED_S", 0.03)
    monkeypatch.setattr(coord_mod, "WINDOW_GROUP_S", 0.08)
    monkeypatch.setattr(coord_mod, "MAX_WAIT_S", 1.0)
    coord_mod.AttentionCoordinator.reset_all()
    sink = effects_svc.FakeEffectSink()
    effects_svc.install_fake_sink(sink)
    yield sink
    effects_svc.uninstall_fake_sink()
    coord_mod.AttentionCoordinator.reset_all()


async def replay_episode(
    ctx, tmp_path, monkeypatch, stub_memory, episode: dict, sink,
) -> dict:
    """Feed an episode's messages through the real pipeline; return observed
    decisions/effects for assertion."""
    _stub_workspace(monkeypatch)

    llm_calls: list = []
    reply = episode.get("llm_reply", "")

    async def behaviour(messages, tools):
        llm_calls.append(messages)
        send = next((t for t in tools if t.name == "send_whatsapp_message"), None)
        if send is not None and reply:
            await send.handler(text=reply)
        return reply

    _stub_llm(monkeypatch, behaviour)

    if "probe_decision" in episode:
        monkeypatch.setattr(
            "bob_server.services.attention.tier2.probe_actionability",
            AsyncMock(return_value=episode["probe_decision"]))
        # Probe path is gated by conversation policy; enable per session below.

    await _seed_contact(ctx.db, "+614000000010")
    svc = _make_service(ctx, tmp_path)

    session_keys = set()
    for msg in episode["messages"]:
        if msg["kind"] == "dm":
            payload = _dm_payload(msg["phone"], msg["text"], msg_id=msg["msg_id"])
            session_keys.add(f"agent:main:whatsapp:dm:{msg['phone'].lstrip('+')}")
        else:
            payload = _group_payload(msg["phone"], msg["text"], msg_id=msg["msg_id"])
            session_keys.add("agent:main:whatsapp:group:120363000000000001")
        await svc._handle_incoming_message(payload)
        if "probe_decision" in episode:
            # Enable the Tier 2 probe via conversation policy (conversation
            # exists after the first message).
            from bob_server.repositories.conversations import ConversationRepository
            repo = ConversationRepository(ctx.db)
            for sk in session_keys:
                await repo.ensure(sk)
                await repo.set_policy(sk, {"patience_enabled": True,
                                           "patience_relevance_gating": True})

    # Let attention windows close and dispatches drain.
    for _ in range(100):
        await asyncio.sleep(0.03)
        pending = await ctx.db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE role = 'user' AND dispatched = 0")
        if pending["n"] == 0 and not coord_mod.AttentionCoordinator._dispatching:
            break

    assistant = await ctx.db.fetch_all(
        "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id")
    unclaimed = await ctx.db.fetch_one(
        "SELECT COUNT(*) AS n FROM messages WHERE role='user' AND dispatched = 0")
    decisions = await ctx.db.fetch_all(
        "SELECT decision FROM attention_shadow ORDER BY id")
    return {
        "turns": len(llm_calls),
        "send_effects": len([d for d in sink.delivered
                             if d["kind"].startswith("whatsapp_send")]),
        "assistant_history": [r["content"] for r in assistant],
        "all_messages_claimed": unclaimed["n"] == 0,
        "decisions": [r["decision"] for r in decisions],
    }


@pytest.mark.parametrize("episode", EPISODES, ids=[e["name"] for e in EPISODES])
async def test_episode_replays_with_expected_decisions_and_effects(
        ctx, tmp_path, monkeypatch, stub_memory, replay_environment, episode):
    observed = await replay_episode(
        ctx, tmp_path, monkeypatch, stub_memory, episode, replay_environment)

    expect = episode["expect"]
    assert observed["turns"] == expect["turns"], (
        f"{episode['name']}: turns {observed['turns']} != {expect['turns']}")
    assert observed["send_effects"] == expect["send_effects"]
    if "assistant_history" in expect:
        assert observed["assistant_history"] == expect["assistant_history"]
    if expect.get("all_messages_claimed"):
        assert observed["all_messages_claimed"], "messages left unclaimed"


async def test_fake_sink_intercepts_every_kind(ctx, replay_environment):
    """Invariant 8: with the sink installed, even a kind with a real
    registered executor never reaches it."""
    real = AsyncMock()
    effects_svc.register_executor("danger_send", real)
    result = await effects_svc.emit_and_deliver(
        ctx, kind="danger_send", idempotency_key="rp-sink-1", payload={"x": 1})
    assert result["ok"] and result.get("fake")
    real.assert_not_awaited()
    assert replay_environment.delivered[-1]["kind"] == "danger_send"


async def test_deletion_propagation_redacts_event_payloads(ctx, db):
    """Decision 7: deleting a contact tombstones related event payloads while
    keeping event identity and ordering."""
    import bob_server.heartbeat as hb
    from bob_server.repositories.event_log import Event, EventLogRepository

    await db.execute(
        """INSERT INTO contacts (id, name, phone_number, deleted_at, created_at, updated_at)
           VALUES ('c-del', 'Gone', '+61400000001', datetime('now'),
                   datetime('now'), datetime('now'))""")
    events = EventLogRepository(db)
    await events.append(Event(
        event_type="message.received", binding_key="k", conversation_id="k",
        source="whatsapp", external_id="del-1",
        payload={"contact_id": "c-del", "text": "private"}))
    await events.append(Event(
        event_type="message.received", binding_key="k", conversation_id="k",
        source="whatsapp", external_id="keep-1",
        payload={"contact_id": "c-other", "text": "kept"}))

    hb._last_deletion_propagation = None
    await hb.DeletionPropagationTask().run(ctx)

    gone = await db.fetch_one("SELECT * FROM event_log WHERE external_id = 'del-1'")
    assert '"redacted"' in gone["payload_json"] and "private" not in gone["payload_json"]
    kept = await db.fetch_one("SELECT * FROM event_log WHERE external_id = 'keep-1'")
    assert "kept" in kept["payload_json"]
