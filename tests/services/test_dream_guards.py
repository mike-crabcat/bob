"""Guards against the 2026-09-02 stale-announcement incident:

1. Terminal dream items must not reopen on system-generated evidence (goal-
   progress headers, routine injections, Bob's own replies) — only on lines a
   human actually said.
2. The announce fact-check expires plans whose premise no longer holds
   instead of announcing them ("coffee still needs a date" after the coffee).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

SK = "agent:main:whatsapp:group:120363060000000000"


def _iso(**delta: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


async def _make_run(store) -> str:
    return await store.create_run(
        trigger="cli", window_start=_iso(days=-1), window_end=_iso(), model="test")


@pytest.mark.asyncio
async def test_evidence_has_human(ctx):
    from bob_server.services.dream.models import Evidence
    from bob_server.services.dream.runner import DreamRunner

    system_only = [Evidence(kind="observed", session_key=SK, at=_iso(minutes=-1), by="")]
    human = system_only + [Evidence(kind="observed", session_key=SK, at=_iso(minutes=-1), by="7c9f0fd7")]

    assert DreamRunner._evidence_has_human(system_only) is False
    assert DreamRunner._evidence_has_human(human) is True


@pytest.mark.asyncio
async def test_terminal_plan_does_not_reopen_on_system_evidence(ctx, monkeypatch):
    from bob_server.services.dream.models import Evidence, PlanCandidate
    from bob_server.services.dream.runner import DreamRunner
    from bob_server.services.dream.store import DreamStore

    store = DreamStore(ctx)
    old_run = await _make_run(store)
    done_id = await store.insert_plan(PlanCandidate(
        title="AI Doom group coffee",
        what_was_discussed="coffee needs a date",
        proposed_action="agree a date and venue",
        assistance_method="ask the group",
        evidence=[Evidence(kind="observed", session_key=SK, at=_iso(days=-3), by="7c9f0fd7")],
    ), run_id=old_run, status="completed", session_key=SK)

    async def fake_dedup(self, item_type, title, body):
        return None, await store.get_plan(done_id), None

    monkeypatch.setattr(DreamRunner, "_dedup_lookup", fake_dedup)
    runner = DreamRunner(ctx)

    def candidate(by: str) -> PlanCandidate:
        # Both stamped after the terminal plan's updated_at (set at insert),
        # so freshness passes and `by` is the only variable under test.
        return PlanCandidate(
            title="AI Doom group coffee",
            what_was_discussed="coffee needs a date",
            proposed_action="agree a date and venue",
            assistance_method="ask the group",
            evidence=[Evidence(kind="observed", session_key=SK, at=_iso(minutes=1), by=by)],
        )

    # Fresh but system-authored evidence (goal header): suppressed, stays completed.
    stats: dict = {"merged": [], "suppressed": []}
    await runner._write_plan("dream-new", candidate(by=""), SK, stats)
    assert stats["suppressed"] and not stats["merged"]
    assert (await store.get_plan(done_id))["status"] == "completed"

    # Fresh human evidence reopens (timestamp freshness still applies too).
    stats = {"merged": [], "suppressed": []}
    await runner._write_plan("dream-new", candidate(by="7c9f0fd7"), SK, stats)
    assert stats["merged"] and stats["merged"][0].get("reopened") is True
    assert (await store.get_plan(done_id))["status"] == "approved"


@pytest.mark.asyncio
async def test_announce_factcheck_expires_stale_plan(ctx, monkeypatch):
    from bob_server.services.dream.announce import AnnounceService
    from bob_server.services.dream.models import Evidence, PlanCandidate
    from bob_server.services.dream.store import DreamStore
    from bob_server.services.llm_dispatch import LLMDispatchService

    store = DreamStore(ctx)
    old_run = await _make_run(store)
    plan_id = await store.insert_plan(PlanCandidate(
        title="AI Doom group coffee",
        what_was_discussed="the coffee still hasn't got a date or spot sorted",
        proposed_action="agree a date, time and venue",
        assistance_method="ask the group for a day this week",
        evidence=[Evidence(kind="observed", session_key=SK, at=_iso(days=-1), by="7c9f0fd7")],
    ), run_id=old_run, status="approved", approved_by="auto", session_key=SK)

    class FakeBridge:
        connected = True

        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, chat_id: str, message: str) -> None:
            self.sent.append((chat_id, message))

    bridge = FakeBridge()
    ctx.whatsapp_bridge = bridge

    async def fake_chat(self, messages, **kwargs):
        if "stops stale announcements" in messages[0]["content"]:
            return "STALE: coffee already confirmed 1 Sep at Little Stables"
        return "Hey! Quick thought about the coffee."

    monkeypatch.setattr(LLMDispatchService, "chat", fake_chat)

    result = await AnnounceService(ctx).flush()

    assert result["expired_stale"] == 1
    assert result["plans_announced"] == 0
    assert bridge.sent == []
    plan = await store.get_plan(plan_id)
    assert plan["status"] == "expired"
    assert plan["announced_at"] is None
    # Expiry reason recorded on the evidence trail for auditability.
    assert "announce fact-check" in (plan["evidence_json"] or "")


@pytest.mark.asyncio
async def test_announce_factcheck_ok_still_announces(ctx, monkeypatch):
    from bob_server.services.dream.announce import AnnounceService
    from bob_server.services.dream.models import Evidence, PlanCandidate
    from bob_server.services.dream.store import DreamStore
    from bob_server.services.llm_dispatch import LLMDispatchService

    store = DreamStore(ctx)
    old_run = await _make_run(store)
    plan_id = await store.insert_plan(PlanCandidate(
        title="Confirm the weekend hike",
        what_was_discussed="group hasn't picked a trail for Saturday",
        proposed_action="propose two trails and ask for a vote",
        assistance_method="message the group",
        evidence=[Evidence(kind="observed", session_key=SK, at=_iso(minutes=-30), by="7c9f0fd7")],
    ), run_id=old_run, status="approved", approved_by="auto", session_key=SK)

    class FakeBridge:
        connected = True

        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, chat_id: str, message: str) -> None:
            self.sent.append((chat_id, message))

    bridge = FakeBridge()
    ctx.whatsapp_bridge = bridge

    async def fake_chat(self, messages, **kwargs):
        if "stops stale announcements" in messages[0]["content"]:
            return "OK"
        return "Hike proposal message"

    monkeypatch.setattr(LLMDispatchService, "chat", fake_chat)

    result = await AnnounceService(ctx).flush()

    assert result["plans_announced"] == 1
    assert bridge.sent and "Hike proposal message" in bridge.sent[0][1]
    plan = await store.get_plan(plan_id)
    assert plan["status"] == "approved"
    assert plan["announced_at"]
