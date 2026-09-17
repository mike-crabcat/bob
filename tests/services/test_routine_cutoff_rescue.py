"""Routine cutoff rescue (2026-09-14 crypto-report incident).

The 07:00 crypto morning report burned its 120s wall-clock budget
diagnosing a broken chart CLI, wrote its wrap-up as plain text, and never
called the send tool — the report was archived silently (dispatched=1 in
the DB, nothing in WhatsApp). Delivery-intent routines that get cut with
un-sent text now get that text delivered by the runner; session-only
routines keep their silent-by-design outcome.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeBridge:
    def __init__(self):
        self.connected = True
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        return "req-test"


def _ctx(db, tmp_path, bridge):
    return SimpleNamespace(
        db=db,
        settings=SimpleNamespace(
            harness=SimpleNamespace(workspace_dir=tmp_path),
            config_dir=tmp_path,
            openai=SimpleNamespace(default_model="gpt-test-default"),
            openrouter=SimpleNamespace(enabled=True),
        ),
        whatsapp_bridge=bridge,
    )


DELIVERY_PROMPT = (
    "Morning crypto report for the Crypto-Bob channel. Do the following:\n"
    "1. Determine held assets from the ledger.\n"
    "3. Send the charts to this channel with a buy/sell summary.")
SESSION_ONLY_PROMPT = "Tidy the scratch directory and log what was removed."


async def _fire(db, tmp_path, monkeypatch, *, prompt, reply, hit_cutoff,
                sent_via_tool=False):
    from server.repositories.conversations import ConversationRepository
    from server.services import routine_service

    stored: list[dict] = []
    bridge = _FakeBridge()

    class _FakeSessionSvc:
        def __init__(self, ctx):
            pass

        async def add_message(self, session_key, role, content, **k):
            stored.append({"role": role, "content": content})
            return None

    class _FakeDispatch:
        def __init__(self, ctx):
            pass

        async def chat_with_tools(self, messages, tools, **kwargs):
            stats = kwargs.get("budget_stats")
            if stats is not None and hit_cutoff:
                stats["hit_wall_clock"] = True
            if sent_via_tool and bridge.sent == []:
                # simulate the model having called the send tool itself
                await bridge.send_message("1203@g.us", reply)
            return reply

    async def _fake_route_for(self, session_key):
        return None

    async def _fake_mark_run(self, routine_id):
        return None

    async def _fake_build_messages(prompt, user_message, *, system_content=None,
                                   current_model=None,
                                   current_model_override=None, **k):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(
        "server.services.session_service.SessionService", _FakeSessionSvc)
    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService", _FakeDispatch)
    monkeypatch.setattr(ConversationRepository, "route_for", _fake_route_for)
    monkeypatch.setattr(routine_service.RoutineService, "mark_run", _fake_mark_run)
    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _fake_build_messages)

    async def _fake_workspace_prompt(*a, **k):
        return ""

    monkeypatch.setattr(
        "server.services.prompt_assembler.load_workspace_prompt",
        _fake_workspace_prompt)
    monkeypatch.setattr(
        "server.services.tool_registry.build_common_tools",
        lambda *a, **k: [])
    monkeypatch.setattr(
        "server.services.wake_service.session_key_to_chat_id",
        lambda sk: "1203@g.us")

    routine = {
        "id": "r1", "session_key": "wa:123", "name": "report",
        "schedule": "0 7 * * *", "prompt": prompt, "enabled": 1,
        "timezone": "Australia/Perth",
    }
    await routine_service.fire_routine(_ctx(db, tmp_path, bridge), routine)
    return bridge, stored


async def test_cutoff_unsent_report_is_delivered(db, tmp_path, monkeypatch):
    bridge, stored = await _fire(
        db, tmp_path, monkeypatch,
        prompt=DELIVERY_PROMPT,
        reply="**Blocked on the charts — here's where things stand.**",
        hit_cutoff=True)
    assert len(bridge.sent) == 1
    chat_id, text = bridge.sent[0]
    assert chat_id == "1203@g.us"
    assert text.startswith("(auto-delivered — routine hit its time budget")
    assert "Blocked on the charts" in text
    # the report is still archived to the transcript
    assert any(s["role"] == "assistant" and "Blocked" in s["content"]
               for s in stored)


async def test_cutoff_session_only_routine_stays_silent(db, tmp_path, monkeypatch):
    bridge, _ = await _fire(
        db, tmp_path, monkeypatch,
        prompt=SESSION_ONLY_PROMPT, reply="tidied 3 files",
        hit_cutoff=True)
    assert bridge.sent == []


async def test_cutoff_no_reply_not_delivered(db, tmp_path, monkeypatch):
    bridge, _ = await _fire(
        db, tmp_path, monkeypatch,
        prompt=DELIVERY_PROMPT, reply="NO_REPLY", hit_cutoff=True)
    assert bridge.sent == []


async def test_uncut_turn_not_rescued(db, tmp_path, monkeypatch):
    """Normal completion: the model sends (or NO_REPLIES) by itself — the
    rescue must not double-deliver."""
    bridge, _ = await _fire(
        db, tmp_path, monkeypatch,
        prompt=DELIVERY_PROMPT,
        reply="**Morning report.**", hit_cutoff=False, sent_via_tool=True)
    assert len(bridge.sent) == 1  # only the model's own send
    assert "auto-delivered" not in bridge.sent[0][1]


async def test_delivery_intent_regex():
    from server.services.routine_service import _ROUTINE_DELIVERY_INTENT_RE
    # the incident's actual routine prompt
    assert _ROUTINE_DELIVERY_INTENT_RE.search(DELIVERY_PROMPT)
    assert _ROUTINE_DELIVERY_INTENT_RE.search(
        "publish the daily AI news brief to the current AI Doom WhatsApp group")
    assert _ROUTINE_DELIVERY_INTENT_RE.search(
        "post the jingle to the AI doom chat each morning")
    # session-only wording: no channel noun near a delivery verb
    assert not _ROUTINE_DELIVERY_INTENT_RE.search(SESSION_ONLY_PROMPT)
    assert not _ROUTINE_DELIVERY_INTENT_RE.search(
        "log the summary to the session transcript; do not send")


async def test_delivery_suffix_rides_delivery_prompt(db, tmp_path, monkeypatch):
    """Delivery-intent prompts get the report-first contract appended."""
    from server.services import routine_service

    captured: list[str] = []

    class _FakeSessionSvc:
        def __init__(self, ctx):
            pass

        async def add_message(self, session_key, role, content, **k):
            if role == "user":
                captured.append(content)
            return None

    class _FakeDispatch:
        def __init__(self, ctx):
            pass

        async def chat_with_tools(self, messages, tools, **kwargs):
            return "NO_REPLY"

    from server.repositories.conversations import ConversationRepository

    async def _fake_route_for(self, session_key):
        return None

    async def _fake_mark_run(self, routine_id):
        return None

    monkeypatch.setattr(
        "server.services.session_service.SessionService", _FakeSessionSvc)
    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService", _FakeDispatch)
    monkeypatch.setattr(ConversationRepository, "route_for", _fake_route_for)
    monkeypatch.setattr(routine_service.RoutineService, "mark_run", _fake_mark_run)
    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages",
        lambda prompt, user_message, **k: [{"role": "user", "content": prompt}])

    async def _fake_workspace_prompt(*a, **k):
        return ""

    monkeypatch.setattr(
        "server.services.prompt_assembler.load_workspace_prompt",
        _fake_workspace_prompt)
    monkeypatch.setattr(
        "server.services.tool_registry.build_common_tools",
        lambda *a, **k: [])
    monkeypatch.setattr(
        "server.services.wake_service.session_key_to_chat_id",
        lambda sk: None)

    await routine_service.fire_routine(
        _ctx(db, tmp_path, _FakeBridge()),
        {"id": "r1", "session_key": "wa:123", "name": "report",
         "schedule": "0 7 * * *", "prompt": DELIVERY_PROMPT,
         "enabled": 1, "timezone": "Australia/Perth"})
    assert captured and "Delivery contract:" in captured[0]
