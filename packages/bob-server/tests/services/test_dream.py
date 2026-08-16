"""Dream system tests: validation, cursors, dedup, announce guards, runner e2e.

LLM calls are stubbed at LLMDispatchService.chat / embed_text so the whole
pipeline runs deterministically against the in-memory DB.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bob_server.services.base import iso_utc
from bob_server.services.dream.models import Evidence, PlanCandidate, ResolutionCandidate

SK = "wa:contact:whatsapp:dm:61412345678"
SK_GROUP = "wa:group:whatsapp:group:1203634"

REVIEW_PAYLOAD = {
    "resolutions": [
        {
            "title": "Answers group questions with unnecessary tool calls",
            "behaviour": "Re-called recall for facts already in the prompt",
            "trigger_condition": "Factual questions in WhatsApp groups",
            "success_signal": "Group factual questions answered without a redundant recall call",
            "evidence": [{"line": 2, "excerpt": "what time does the flight leave"}],
        },
        {
            "title": "Fabricated citation should be rejected",
            "behaviour": "x",
            "trigger_condition": "y",
            "success_signal": "something measurable and concrete here",
            "evidence": [{"line": 99, "excerpt": "not a real line"}],
        },
    ],
    "plans": [
        {
            "title": "Catch up with Sarah",
            "what_was_discussed": "They agreed to catch up soon but never set a date",
            "proposed_action": "Pick a Saturday that works for both",
            "assistance_method": "Check the calendar and propose two Saturdays",
            "autonomy_tier": 1,
            "due_hint": "soon",
            "evidence": [{"line": 4, "excerpt": "we should catch up soon"}],
            "related_entities": [],
        }
    ],
}


def _messages(n: int = 6) -> list[dict]:
    return [
        {"created_at": f"2026-08-16T0{i}:00:00Z", "role": "user" if i % 2 else "assistant",
         "sender_id": "c1", "content": "what time does the flight leave" if i == 2
         else ("we should catch up soon" if i == 4 else f"msg {i}")}
        for i in range(1, n + 1)
    ]



async def _seed_run_row(db) -> None:
    """dream_plans/resolutions have FK source_run_id → dream_runs; seed the row tests reference."""
    await db.execute(
        "INSERT OR IGNORE INTO dream_runs (id, started_at, finished_at, window_start, window_end, status, trigger, model) "
        "VALUES ('dream-x', '2026-08-16T00:00:00Z', '2026-08-16T00:05:00Z', '2026-08-15T00:00:00Z', '2026-08-16T00:00:00Z', 'complete', 'cli', 'test')"
    )

async def _seed_messages(db, session_key: str, messages: list[dict]) -> None:
    for m in messages:
        await db.execute(
            "INSERT INTO session_messages (session_key, role, content, created_at, sender_id, dispatched) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (session_key, m["role"], m["content"], m["created_at"], m.get("sender_id")),
        )


class _StubLLM:
    """Route dream LLM calls to canned payloads."""

    def __init__(self, *, review: dict | None = None, prospective: dict | None = None,
                 announce: str = "nudge text") -> None:
        self.review = review if review is not None else REVIEW_PAYLOAD
        self.prospective = prospective if prospective is not None else {"decisions": []}
        self.announce_text = announce
        self.calls: list[dict] = []

    async def chat(self, messages: list[dict], *, call_category: str, **kwargs: Any) -> str:
        self.calls.append({"category": call_category, "model": kwargs.get("model")})
        system = messages[0]["content"] if messages else ""
        if call_category == "dream_review":
            return json.dumps(self.review)
        if call_category == "dream_prospective":
            return json.dumps(self.prospective)
        if call_category == "dream_synthesis":
            return "Journal: one session reviewed, one plan created."
        return self.announce_text


class _StubBridge:
    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> str:
        self.sent.append((chat_id, text))
        return "req-1"


@pytest.fixture
def stub_env(monkeypatch, ctx):
    """Patch LLM + embeddings + bridge; return handles for assertions."""
    stub_llm = _StubLLM()
    stub_bridge = _StubBridge()
    monkeypatch.setattr("bob_server.services.llm_dispatch.LLMDispatchService.chat", stub_llm.chat)
    monkeypatch.setattr(
        "bob_server.services.memory.embedding.embed_text",
        lambda text: _async_none(),
    )
    ctx.whatsapp_bridge = stub_bridge
    ctx.settings.dream.enabled = True
    ctx.settings.dream.interval_minutes = 0  # never interval-gate in tests
    return stub_llm, stub_bridge


async def _async_none():
    return None


# ---------------------------------------------------------------- validation

async def test_review_validates_evidence_and_rejects_fabricated(ctx, stub_env):
    from bob_server.services.dream.review import ReviewService

    result = await ReviewService(ctx).review_session(session_key=SK, messages=_messages())
    # fabricated line 99 rejected; valid line 2 accepted
    assert len(result["resolutions"]) == 1
    assert result["stats"]["rejected_invalid"] == 1
    assert result["resolutions"][0].evidence[0].line == 2


async def test_review_rejects_vacuous_success_signal(ctx, stub_env, monkeypatch):
    from bob_server.services.dream.review import ReviewService

    payload = json.loads(json.dumps(REVIEW_PAYLOAD))
    payload["resolutions"][0]["success_signal"] = "be better"
    monkeypatch.setattr(
        "bob_server.services.llm_dispatch.LLMDispatchService.chat",
        _StubLLM(review=payload).chat,
    )
    result = await ReviewService(ctx).review_session(session_key=SK, messages=_messages())
    assert result["resolutions"] == []
    assert result["stats"]["rejected_invalid"] == 2  # vacuous + fabricated


# ------------------------------------------------------------- runner e2e

async def test_runner_end_to_end_creates_items_and_advances_cursor(ctx, stub_env):
    from bob_server.services.dream.runner import DreamRunner

    await _seed_messages(ctx.db, SK, _messages())
    result = await DreamRunner(ctx).maybe_run(trigger="cli")
    assert result is not None
    stats = result["stats"]
    assert len(stats["sessions"]) == 1
    assert len(stats["plans_created"]) == 1
    assert len(stats["resolutions_created"]) == 1

    plans = await ctx.db.fetch_all("SELECT * FROM dream_plans")
    assert len(plans) == 1
    assert plans[0]["status"] in ("draft", "proposed")  # autoplan off, draft mode default
    assert plans[0]["announced_at"] is None

    resolutions = await ctx.db.fetch_all("SELECT * FROM dream_resolutions")
    assert resolutions[0]["status"] == "draft"
    assert resolutions[0]["observation_count"] == 1

    # cursor advanced: a second run finds nothing due
    result2 = await DreamRunner(ctx).maybe_run(trigger="cli")
    assert result2["stats"]["sessions"] == []
    assert result2["stats"]["plans_created"] == []

    # review passes used the memory model
    stub_llm, _ = stub_env
    models = {c["model"] for c in stub_llm.calls}
    assert models == {ctx.settings.openai.get_memory_model()}


async def test_runner_autoapprove_with_backlog_guard(ctx, stub_env, monkeypatch):
    from bob_server.services.dream import config as dream_config
    from bob_server.services.dream.runner import DreamRunner

    await dream_config.set_auto_approve_plans(ctx.db, True)

    fresh = _messages()
    await _seed_messages(ctx.db, SK, fresh)
    await DreamRunner(ctx).maybe_run(trigger="cli")
    plans = await ctx.db.fetch_all("SELECT * FROM dream_plans")
    assert plans[0]["status"] == "approved" and plans[0]["approved_by"] == "auto"

    # backlog: old evidence never auto-approves even with autoplan on
    old = _messages()
    for i, m in enumerate(old, 1):
        m["created_at"] = f"2026-08-01T0{i}:00:00Z"
    await _seed_messages(ctx.db, SK_GROUP, old)
    await DreamRunner(ctx).maybe_run(trigger="cli")
    row = await ctx.db.fetch_one("SELECT * FROM dream_plans WHERE id LIKE '%' AND source_run_id != ?", (plans[0]["source_run_id"],))
    assert row is None or row["status"] in ("draft", "proposed")  # lookback(14d) covers Aug 1 → guarded


async def test_dedup_merge_on_reobservation(ctx, stub_env, monkeypatch):
    from bob_server.services.dream.runner import DreamRunner

    async def _const_vec(text):
        return [0.1] * 1536

    monkeypatch.setattr("bob_server.services.memory.embedding.embed_text", _const_vec)

    await _seed_messages(ctx.db, SK, _messages())
    await DreamRunner(ctx).maybe_run(trigger="cli")

    # re-observe the same things in a second window → constant vectors are
    # identical, so candidates merge into the existing items instead of duplicating
    later = _messages()
    for i, m in enumerate(later, 1):
        m["created_at"] = f"2026-08-16T1{i}:00:00Z"
        m["content"] = m["content"] + " again"
    await _seed_messages(ctx.db, SK, later)
    await DreamRunner(ctx).maybe_run(trigger="cli")

    plans = await ctx.db.fetch_all("SELECT * FROM dream_plans")
    resolutions = await ctx.db.fetch_all("SELECT * FROM dream_resolutions")
    assert len(plans) == 1  # merged, not duplicated
    assert len(resolutions) == 1
    assert resolutions[0]["observation_count"] == 2


async def test_recently_terminal_suppression_and_reopen(ctx, stub_env):
    from bob_server.services.dream.runner import DreamRunner

    async def _const_vec(text):
        return [0.2] * 1536

    import bob_server.services.memory.embedding as emb

    original = emb.embed_text
    emb.embed_text = _const_vec
    try:
        await _seed_messages(ctx.db, SK, _messages())
        await DreamRunner(ctx).maybe_run(trigger="cli")
        await ctx.db.execute("UPDATE dream_plans SET status = 'dismissed', updated_at = ? WHERE 1=1", (iso_utc(),))

        # same candidate again → suppressed (terminal within 14d, evidence not newer)
        later = _messages()
        for i, m in enumerate(later, 1):
            m["created_at"] = f"2026-08-16T0{i}:00:05Z"
        await ctx.db.execute("DELETE FROM dream_session_review WHERE session_key = ?", (SK,))
        await ctx.db.execute("DELETE FROM session_messages WHERE session_key = ?", (SK,))
        await _seed_messages(ctx.db, SK, later)
        result = await DreamRunner(ctx).maybe_run(trigger="cli")
        assert len(result["stats"]["suppressed"]) >= 1
        plans = await ctx.db.fetch_all("SELECT * FROM dream_plans")
        assert len(plans) == 1  # no duplicate created
    finally:
        emb.embed_text = original


# ------------------------------------------------------------- prospective

async def test_prospective_engagement_guard_blocks_expiry(ctx, stub_env, monkeypatch):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.prospective import ProspectiveService

    plan_id = "plan-test0001"
    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, approved_by, approved_at, announced_at, evidence_json, source_run_id,
             due_hint, created_at, updated_at)
           VALUES (?, 't', 'd', 'a', 'm', 'approved', 'auto', ?, ?, ?, 'dream-x', 'last week', ?, ?)""",
        (plan_id, now, now, json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    # engagement: user replied after the announcement
    await _seed_messages(ctx.db, SK, [
        {"created_at": "2026-08-16T12:00:00Z", "role": "user", "sender_id": "c1", "content": "yes saturday works"},
    ])

    prospective_payload = {"decisions": [{"item_type": "plan", "item_id": plan_id, "action": "expire", "reason": "due passed"}]}
    monkeypatch.setattr(
        "bob_server.services.llm_dispatch.LLMDispatchService.chat",
        _StubLLM(prospective=prospective_payload).chat,
    )
    await ProspectiveService(ctx).run(run_id="dream-x", settings=ctx.settings.dream)
    plan = await ctx.db.fetch_one("SELECT status FROM dream_plans WHERE id = ?", (plan_id,))
    assert plan["status"] == "approved"  # engagement blocked expiry


async def test_resolution_kept_requires_consecutive_signals(ctx, stub_env, monkeypatch):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.prospective import ProspectiveService

    res_id = "resolution-ke1"
    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_resolutions (id, title, behaviour, trigger_condition, success_signal,
             status, first_seen_at, last_seen_at, observation_count, evidence_json, source_run_id)
           VALUES (?, 't', 'b', 'tr', 'sig', 'open', ?, ?, 2, ?, 'dream-x')""",
        (res_id, now, now, json.dumps([{"kind": "observed"}, {"kind": "kept_signal"}])),
    )
    payload = {"decisions": [{"item_type": "resolution", "item_id": res_id, "action": "kept", "reason": "signal observed"}]}
    monkeypatch.setattr(
        "bob_server.services.llm_dispatch.LLMDispatchService.chat",
        _StubLLM(prospective=payload).chat,
    )
    settings = ctx.settings.dream
    settings.resolution_kept_consecutive_runs = 3
    await ProspectiveService(ctx).run(run_id="dream-x", settings=settings)
    row = await ctx.db.fetch_one("SELECT status, evidence_json FROM dream_resolutions WHERE id = ?", (res_id,))
    assert row["status"] == "open"  # 2 prior signals + this one = 3, but applied as signal then kept? count: prior=1 → 1+1=2 < 3
    evidence = json.loads(row["evidence_json"])
    assert any(e["kind"] == "kept_signal" for e in evidence)

    # third consecutive signal now reaches the threshold
    await ProspectiveService(ctx).run(run_id="dream-y", settings=settings)
    row = await ctx.db.fetch_one("SELECT status FROM dream_resolutions WHERE id = ?", (res_id,))
    assert row["status"] == "kept"


# ------------------------------------------------------------- announcements

async def test_announce_flush_batches_guards_and_records(ctx, stub_env):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.announce import AnnounceService

    now = iso_utc()
    for i in range(2):
        await ctx.db.execute(
            """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
                 status, approved_by, approved_at, evidence_json, source_run_id, created_at, updated_at)
               VALUES (?, 't', 'd', 'a', 'm', 'approved', 'auto', NULL, ?, 'dream-x', ?, ?)""",
            (f"plan-a{i}", json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
        )
    result = await AnnounceService(ctx).flush()
    assert result["plans_announced"] == 2 and result["sessions"] == 1  # one batched message

    # recorded with synthetic + metadata marker; announced_at set
    msg = await ctx.db.fetch_one(
        "SELECT * FROM session_messages WHERE session_key = ? AND metadata LIKE '%dream_announce%'", (SK,)
    )
    assert msg is not None and msg["synthetic"] == 1
    plans = await ctx.db.fetch_all("SELECT announced_at FROM dream_plans")
    assert all(p["announced_at"] for p in plans)

    # idempotent: second flush sends nothing
    stub_llm, bridge = stub_env
    sent_before = len(bridge.sent)
    result2 = await AnnounceService(ctx).flush()
    assert result2["plans_announced"] == 0 and len(bridge.sent) == sent_before


async def test_announce_hot_session_defer(ctx, stub_env):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.announce import AnnounceService

    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, approved_by, approved_at, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-hot1', 't', 'd', 'a', 'm', 'approved', 'auto', NULL, ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    # inbound message seconds ago → hot → defer
    await _seed_messages(ctx.db, SK, [
        {"created_at": iso_utc(), "role": "user", "sender_id": "c1", "content": "talking right now"},
    ])
    result = await AnnounceService(ctx).flush()
    assert result["deferred_hot"] == 1 and result["plans_announced"] == 0


async def test_announce_daily_cap(ctx, stub_env):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.announce import AnnounceService

    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, approved_by, approved_at, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-cap1', 't', 'd', 'a', 'm', 'approved', 'auto', NULL, ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    for _ in range(ctx.settings.dream.announce_daily_cap_per_session):
        await ctx.db.execute(
            "INSERT INTO session_messages (session_key, role, content, created_at, synthetic, metadata, dispatched) "
            "VALUES (?, 'assistant', 'earlier announce', ?, 1, '{\"dream_announce\":[\"plan-old\"]}', 1)",
            (SK, iso_utc()),
        )
    result = await AnnounceService(ctx).flush()
    assert result["deferred_cap"] == 1 and result["plans_announced"] == 0


async def test_reannounce_single_followup_cap(ctx, stub_env):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.announce import AnnounceService
    from bob_server.services.dream.prospective import ProspectiveService

    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, approved_by, approved_at, announced_at, evidence_json, source_run_id, due_hint, created_at, updated_at)
           VALUES ('plan-rea1', 't', 'd', 'a', 'm', 'approved', 'auto', ?, ?, ?, 'dream-x', 'soon', ?, ?)""",
        (now, now, json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    # make announced_at older than reannounce_after_days
    await ctx.db.execute(
        "UPDATE dream_plans SET announced_at = datetime('now', '-10 days') WHERE id = 'plan-rea1'"
    )
    payload = {"decisions": [{"item_type": "plan", "item_id": "plan-rea1", "action": "reannounce", "reason": "silent"}]}
    import bob_server.services.llm_dispatch as ld

    stub = _StubLLM(prospective=payload)
    import bob_server.services.dream.prospective as pros

    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setattr(ld.LLMDispatchService, "chat", stub.chat)
    try:
        stats = await ProspectiveService(ctx).run(run_id="dream-x", settings=ctx.settings.dream)
        assert "plan-rea1" in stats["reannounce"]
        row = await ctx.db.fetch_one("SELECT reannounced_at FROM dream_plans WHERE id = 'plan-rea1'")
        assert row["reannounced_at"] is not None

        # second prospective pass cannot spend it again
        stats2 = await ProspectiveService(ctx).run(run_id="dream-y", settings=ctx.settings.dream)
        assert "plan-rea1" not in stats2["reannounce"]
    finally:
        monkeypatch_holder.undo()


# ------------------------------------------------------------- plan tools

async def test_plan_tools_session_bound(ctx):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.tools import make_dream_tools

    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-tb1', 't', 'd', 'a', 'm', 'approved', ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    await ctx.db.execute(
        "INSERT INTO dream_item_links (item_type, item_id, session_key) VALUES ('plan', 'plan-tb1', ?)",
        (SK,),
    )

    tools = {t.name: t for t in make_dream_tools(ctx, session_key=SK)}
    listing = json.loads(await tools["list_plans"].handler())
    assert any("plan-tb1" in p for p in listing["plans"])

    out = json.loads(await tools["plan_cancel"].handler("they said it's off"))
    assert out["ok"] is True
    row = await ctx.db.fetch_one("SELECT status, evidence_json FROM dream_plans WHERE id = 'plan-tb1'")
    assert row["status"] == "dismissed"
    assert json.loads(row["evidence_json"])[-1]["kind"] == "cancelled"

    # outsider session sees nothing and cannot touch the plan
    outsider = {t.name: t for t in make_dream_tools(ctx, session_key="wa:contact:whatsapp:dm:61400000000")}
    listing2 = json.loads(await outsider["list_plans"].handler())
    assert listing2["plans"] == []
    out2 = json.loads(await outsider["plan_cancel"].handler("cancel it", "plan-tb1"))
    assert out2["ok"] is False


async def test_plan_update_progress_moves_to_actioned(ctx):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.tools import make_dream_tools

    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-pu1', 't', 'd', 'a', 'm', 'approved', ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    await ctx.db.execute(
        "INSERT INTO dream_item_links (item_type, item_id, session_key) VALUES ('plan', 'plan-pu1', ?)", (SK,)
    )
    tools = {t.name: t for t in make_dream_tools(ctx, session_key=SK)}
    out = json.loads(await tools["plan_update"].handler(progress="checked calendar, proposed two Saturdays"))
    assert out["ok"] is True
    row = await ctx.db.fetch_one("SELECT status, evidence_json FROM dream_plans WHERE id = 'plan-pu1'")
    assert row["status"] == "actioned"
    assert any(e["kind"] == "progress" for e in json.loads(row["evidence_json"]))


# ------------------------------------------------------------- injection

async def test_injection_gated_and_compact(ctx):
    await _seed_run_row(ctx.db)
    from bob_server.services.dream.injection import build_session_plans_prompt

    now = iso_utc()
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-in1', 'Book Mama San', 'dinner discussed', 'book Saturday', 'check calendar',
             'approved', ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    await ctx.db.execute(
        "INSERT INTO dream_item_links (item_type, item_id, session_key) VALUES ('plan', 'plan-in1', ?)", (SK,)
    )

    off = await build_session_plans_prompt(ctx.db, SK, dream_enabled=False)
    assert off == ""

    on = await build_session_plans_prompt(ctx.db, SK, dream_enabled=True)
    assert "Open Plans for this Session" in on and "plan-in1" in on

    # drafts never show
    await ctx.db.execute("UPDATE dream_plans SET status = 'draft' WHERE id = 'plan-in1'")
    hidden = await build_session_plans_prompt(ctx.db, SK, dream_enabled=True)
    assert hidden == ""


# ------------------------------------------------------------- heartbeat

async def test_dream_task_does_not_block_heartbeat(ctx, stub_env, monkeypatch):
    from bob_server.heartbeat import DreamTask

    await _seed_messages(ctx.db, SK, _messages())

    started = []

    async def _slow_run(self, trigger="heartbeat"):
        import asyncio

        started.append(True)
        await asyncio.sleep(0.25)
        return {"run_id": "dream-x", "stats": {}}

    import bob_server.services.dream.runner as runner_mod

    monkeypatch.setattr(runner_mod.DreamRunner, "run", _slow_run)
    task = DreamTask()
    task._last_check_at = -1e9  # force the check gate open
    import time as _time

    t0 = _time.monotonic()
    await task.run(ctx)  # must return fast, not wait for the dream
    assert _time.monotonic() - t0 < 0.2
    await asyncio.sleep(0.4)
    assert started  # the background run did execute


import asyncio  # noqa: E402  (used by the test above)


async def test_recall_augmentation_appends_dream_items(ctx, db):
    from bob_server.services.memory.tools import recall

    await _seed_run_row(db)
    now = iso_utc()
    await db.execute(
        """INSERT INTO memory_entities (entity_id, entity_type, display_name, status, created_at, updated_at)
           VALUES ('person-sarah-test', 'person', 'Sarah Test', 'active', ?, ?)""", (now, now),
    )
    await db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-rec1', 'Catch up with Sarah', 'd', 'pick a Saturday', 'check calendar',
             'approved', ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": SK}]), now, now),
    )
    await db.execute(
        "INSERT INTO dream_item_links (item_type, item_id, entity_id) VALUES ('plan', 'plan-rec1', 'person-sarah-test')"
    )
    rendered = await recall(db, "person-sarah-test")
    assert "Open dream items" in rendered and "plan-rec1" not in rendered  # human line, id not required
    assert "Catch up with Sarah" in rendered

    # dismissed plans never surface
    await db.execute("UPDATE dream_plans SET status = 'dismissed' WHERE id = 'plan-rec1'")
    rendered2 = await recall(db, "person-sarah-test")
    assert "Open dream items" not in rendered2
