"""Utility conversations tests — docs/utility-conversations-plan.md.

Covers: migration shape (table + valve columns + route_id), repositories,
router valves (hours window parse, cooldown, budget, throttled-never-retried,
fan-out delivery, utility liveness + master kill switch), the per-turn spec
(charter + model alias), and the request tool flow (inert-until-approved,
deny deletes, widening re-approves, narrowing applies autonomously).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from server.config import Settings, UtilityConversationSettings
from server.repositories.stimulus import StimulusRepository
from server.repositories.utility_conversations import (
    UtilityConversationRepository, utility_session_key,
)
from server.services.stimulus_router import _in_hours, drain
from server.services.utility_conversations import (
    _on_approved, _on_rejected, make_sensation_route_tools,
    utility_turn_spec,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "server" / "schemas"
LOCAL_TZ = ZoneInfo("Australia/Perth")

CHARTER = ("Person at doorbell or driveway: check what Sonos is doing; if "
           "den and portable are idle, put Bob FM on both. Never living-room.")


@pytest.fixture
async def db():
    from server.database import Database
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


@pytest.fixture
async def ctx(db, tmp_path):
    from server.context import AppContext
    return AppContext(db=db, settings=Settings(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db"))


@pytest.fixture
def fake_wake(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _wake(ctx, session_key, content, **kw):
        calls.append((session_key, content))
        return True

    import server.services.wake_service as ws
    monkeypatch.setattr(ws, "wake_conversation", _wake)
    return calls


@pytest.fixture
def approvals_emitted(monkeypatch):
    emitted: list[dict] = []

    async def _emit(ctx, *, kind, idempotency_key, payload, turn_id=None):
        emitted.append({"kind": kind, "payload": payload})
        return {"ok": True, "external_result_id": f"ap{len(emitted)}"}

    import server.services.effects as effects
    monkeypatch.setattr(effects, "emit_and_deliver", _emit)
    return emitted


async def _seed_event(db, **over):
    kw = dict(source="frigate", type_="activity.person", level="action",
              ts=datetime.now(timezone.utc).isoformat(),
              dedup_key=None, ttl_s=900, target_hint=None,
              summary="person at doorbell 17:03", body={"camera": "doorbell"})
    kw.update(over)
    if kw["dedup_key"] is None:
        kw["dedup_key"] = f"k-{kw['type_']}-{kw['ts']}-{len(str(kw))}"
    return await StimulusRepository(db).insert_event(**kw)


async def _guard_route(db):
    """The frigate→guard route exists only in prod (seeded by hand after
    002); tests create their own."""
    s_repo = StimulusRepository(db)
    route_id = await s_repo.insert_route(
        source="frigate", type_pattern="activity.*", level="action",
        target_session="agent:main:whatsapp:group:120363411751991047",
        note="", created_by="test")
    await s_repo.set_route_enabled(route_id, True)
    return route_id


async def _make_utility(db, *, name="arrival-herald", enabled=1,
                         charter=CHARTER, valves=True):
    u_repo = UtilityConversationRepository(db)
    target = utility_session_key(name)
    await u_repo.upsert(session_key=target, title=name, charter=charter,
                        created_by="agent:main:whatsapp:dm:61400000000")
    if not enabled:
        await u_repo.set_enabled(target, False)
    route_id = await StimulusRepository(db).insert_route(
        source="frigate", type_pattern="activity.person", level="action",
        target_session=target,
        hours="09:00-21:00" if valves else None,
        cooldown_s=1800 if valves else None,
        budget_per_hour=4 if valves else None,
        note="", created_by="test")
    await StimulusRepository(db).set_route_enabled(route_id, True)
    return target, route_id


def _window_excluding_now() -> str:
    start = (datetime.now(LOCAL_TZ) + timedelta(minutes=70)).replace(minute=0)
    return f"{start.hour:02d}:00-{(start.hour + 1) % 24:02d}:00"


def _window_including_now() -> str:
    now = datetime.now(LOCAL_TZ)
    return f"{now.hour:02d}:00-{(now.hour + 1) % 24:02d}:00"


# ─── migration ────────────────────────────────────────────────────────

async def test_migration_shape(db):
    u = await UtilityConversationRepository(db).upsert(
        session_key="agent:x:utility", title="x", charter="c" * 40,
        created_by="t")
    assert u["model_alias"] == "cheap" and u["enabled"] == 1
    cols = {r["name"] for r in await db.fetch_all(
        "PRAGMA table_info(stimulus_routes)")}
    assert {"hours", "cooldown_s", "budget_per_hour"} <= cols
    ecols = {r["name"] for r in await db.fetch_all(
        "PRAGMA table_info(stimulus_events)")}
    assert "route_id" in ecols


# ─── router valves ────────────────────────────────────────────────────

def test_in_hours_windows():
    dt = lambda h, m: datetime(2026, 9, 10, h, m, tzinfo=LOCAL_TZ)  # noqa: E731
    assert _in_hours(dt(10, 0), "09:00-21:00")
    assert not _in_hours(dt(22, 0), "09:00-21:00")
    assert _in_hours(dt(23, 30), "22:00-06:00")   # wraps midnight
    assert _in_hours(dt(3, 0), "22:00-06:00")
    assert not _in_hours(dt(12, 0), "22:00-06:00")
    assert _in_hours(dt(12, 0), None)             # no valve
    assert _in_hours(dt(12, 0), "garbage")        # unparseable reads 24h


async def test_drain_null_valves_unchanged_and_stamps_route_id(db, ctx, fake_wake):
    guard_route_id = await _guard_route(db)
    await _seed_event(db, dedup_key="v1")
    counts = await drain(ctx)
    assert counts["steered"] == 1 and counts["throttled"] == 0
    frigate_route = await db.fetch_one(
        "SELECT * FROM stimulus_routes WHERE source = 'frigate'")
    assert frigate_route["id"] == guard_route_id
    row = await db.fetch_one("SELECT route_id FROM stimulus_events "
                             "WHERE dedup_key = 'v1'")
    assert row["route_id"] == frigate_route["id"]


async def test_drain_cooldown_throttles_second_fire(db, ctx, fake_wake):
    await _make_utility(db, valves=False)
    await StimulusRepository(db).db.execute(  # clear valve-less → set cooldown
        "UPDATE stimulus_routes SET cooldown_s = 1800 "
        "WHERE target_session = 'agent:arrival-herald:utility'")
    await _seed_event(db, dedup_key="c1")
    assert (await drain(ctx))["steered"] == 1
    await _seed_event(db, dedup_key="c2")
    counts = await drain(ctx)
    assert counts["throttled"] == 1 and counts["steered"] == 0
    assert len(fake_wake) == 1  # throttled never retries
    row = await db.fetch_one("SELECT delivered_steer FROM stimulus_events "
                             "WHERE dedup_key = 'c2'")
    assert row["delivered_steer"] == "throttled"


async def test_drain_budget_exhaustion(db, ctx, fake_wake):
    await _make_utility(db, valves=False)
    await StimulusRepository(db).db.execute(
        "UPDATE stimulus_routes SET budget_per_hour = 1 "
        "WHERE target_session = 'agent:arrival-herald:utility'")
    await _seed_event(db, dedup_key="b1")
    assert (await drain(ctx))["steered"] == 1
    await _seed_event(db, dedup_key="b2")
    assert (await drain(ctx))["throttled"] == 1


async def test_drain_hours_valve(db, ctx, fake_wake):
    await _make_utility(db, valves=False)
    await StimulusRepository(db).db.execute(
        "UPDATE stimulus_routes SET hours = ? "
        "WHERE target_session = 'agent:arrival-herald:utility'",
        (_window_excluding_now(),))
    await _seed_event(db, dedup_key="h1")
    assert (await drain(ctx))["throttled"] == 1 and not fake_wake
    # in-window passes
    await StimulusRepository(db).db.execute(
        "UPDATE stimulus_routes SET hours = ? "
        "WHERE target_session = 'agent:arrival-herald:utility'",
        (_window_including_now(),))
    await _seed_event(db, dedup_key="h2")
    assert (await drain(ctx))["steered"] == 1


async def test_drain_fan_out_to_guard_and_utility(db, ctx, fake_wake):
    await _guard_route(db)                       # activity.* → guard group
    target, _ = await _make_utility(db, valves=False)  # NULL valves — the
    # hours valve would otherwise block whenever the test runs outside
    # 09:00-21:00 local (that case has its own test)
    await _seed_event(db, dedup_key="f1", type_="activity.person")
    counts = await drain(ctx)
    assert counts["steered"] == 2  # one event → two targets
    targets = {t for t, _ in fake_wake}
    assert targets == {"agent:main:whatsapp:group:120363411751991047", target}


async def test_drain_dead_utility_is_log_only(db, ctx, fake_wake):
    target, route_id = await _make_utility(db)
    await UtilityConversationRepository(db).set_enabled(target, False)
    await _seed_event(db, dedup_key="d1")
    counts = await drain(ctx)
    assert counts["steered"] == 0 and counts["logged"] == 1 and not fake_wake
    row = await db.fetch_one("SELECT delivered_steer FROM stimulus_events "
                             "WHERE dedup_key = 'd1'")
    assert row["delivered_steer"] == "log-only"


async def test_drain_master_kill_switch(db, tmp_path, monkeypatch, fake_wake):
    from server.context import AppContext
    from server.database import Database
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    dead_ctx = AppContext(db=database, settings=Settings(
        data_dir=tmp_path / "d", config_dir=tmp_path / "c",
        db_path=tmp_path / "d" / "bob.db",
        utility_conversations=UtilityConversationSettings(enabled=False)))
    await _make_utility(database)
    await _seed_event(database, dedup_key="k1")
    counts = await drain(dead_ctx)
    assert counts["steered"] == 0 and counts["logged"] == 1 and not fake_wake
    await database.close()


# ─── per-turn spec ────────────────────────────────────────────────────

async def test_utility_turn_spec_charter_and_model(db, ctx):
    target, _ = await _make_utility(db)
    spec = await utility_turn_spec(ctx, target)
    assert spec is not None
    charter_block, model = spec
    assert charter_block.startswith("[Charter: arrival-herald]\n")
    assert CHARTER in charter_block
    assert model  # alias resolves (pass-through without models.yaml)


async def test_utility_turn_spec_gates(db, ctx):
    target, _ = await _make_utility(db)
    await UtilityConversationRepository(db).set_enabled(target, False)
    assert await utility_turn_spec(ctx, target) is None
    assert await utility_turn_spec(ctx, "agent:never-made:utility") is None
    ctx.settings.utility_conversations.enabled = False
    await UtilityConversationRepository(db).set_enabled(target, True)
    assert await utility_turn_spec(ctx, target) is None


# ─── request tool flow ────────────────────────────────────────────────

def _tools(ctx):
    return {t.name: t for t in make_sensation_route_tools(
        ctx, "agent:main:whatsapp:group:guard")}


async def _request(ctx, **over):
    tools = _tools(ctx)
    kw = dict(name="arrival-herald", source="frigate",
              type_pattern="activity.person", charter=CHARTER,
              hours="09:00-21:00", cooldown_s=1800, budget_per_hour=4)
    kw.update(over)
    return json.loads(await tools["request_sensation_route"].handler(**kw))


async def test_request_creates_inert_and_emits_approval(db, ctx, approvals_emitted):
    out = await _request(ctx)
    assert out["ok"] and out["status"] == "pending approval"
    route = await StimulusRepository(ctx.db).get_route(out["route_id"])
    assert route["enabled"] == 0  # inert until approved
    assert route["hours"] == "09:00-21:00" and route["cooldown_s"] == 1800
    u = await UtilityConversationRepository(ctx.db).get(
        utility_session_key("arrival-herald"))
    assert u and u["charter"] == CHARTER
    assert len(approvals_emitted) == 1
    payload = approvals_emitted[0]["payload"]
    assert payload["approval_type"] == "sensation_route"
    assert payload["entity_id"] == "arrival-herald"
    assert payload["origin_conversation_id"] == \
        "agent:main:whatsapp:group:guard"  # owner DM unresolvable → requester
    assert "Charter:" in payload["proposal"]["summary"]


async def test_request_validation_caps(db, ctx, approvals_emitted):
    for bad in [
        dict(name="Bad Slug"), dict(name="bad_slug"),
        dict(source="fri gate"), dict(type_pattern="activity person"),
        dict(level="urgent"), dict(charter="too short"),
        dict(hours="9to5"), dict(cooldown_s=60), dict(budget_per_hour=99),
    ]:
        out = await _request(ctx, **{**bad, "name": bad.get("name", "x-slug")})
        assert out["ok"] is False, bad
    assert not approvals_emitted


async def _seed_pending_approval(db, name):
    from server.repositories.approvals import ApprovalRepository
    return await ApprovalRepository(db).create(
        approval_type="sensation_route", entity_id=name,
        title=f"Sensation route: {name}", description="",
        proposal={"name": name}, requested_by="test")


async def test_on_approved_enables_and_reject_deletes(db, ctx, approvals_emitted):
    out = await _request(ctx)
    route_id = out["route_id"]
    target = utility_session_key("arrival-herald")
    proposal = {"route_id": route_id, "name": "arrival-herald",
                "session_key": target}
    row = {"id": "ap1", "status": "approved", "reviewed_by": "owner",
           "requested_by": "agent:main:whatsapp:group:guard",
           "proposal_data": json.dumps(proposal)}
    await _on_approved(ctx, row)
    assert (await StimulusRepository(ctx.db).get_route(route_id))["enabled"] == 1

    # a fresh request, then rejected → inert rows deleted
    out2 = await _request(ctx, name="doorbell-chime")
    row2 = {"id": "ap2", "status": "rejected", "reviewed_by": "owner",
            "requested_by": "r",
            "proposal_data": json.dumps(
                {"route_id": out2["route_id"], "name": "doorbell-chime",
                 "session_key": utility_session_key("doorbell-chime")})}
    await _on_rejected(ctx, row2)
    assert await StimulusRepository(ctx.db).get_route(out2["route_id"]) is None
    assert await UtilityConversationRepository(ctx.db).get(
        utility_session_key("doorbell-chime")) is None
    # the approved route's utility row survives (it has a live route)
    assert await UtilityConversationRepository(ctx.db).get(target)


async def test_on_rejected_never_kills_live_route(db, ctx, approvals_emitted):
    target, route_id = await _make_utility(db)  # enabled/live
    await _on_rejected(ctx, {"id": "ap9", "status": "rejected",
                             "proposal_data": json.dumps(
                                 {"route_id": route_id, "name": "arrival-herald",
                                  "session_key": target})})
    assert (await StimulusRepository(ctx.db).get_route(route_id))["enabled"] == 1


async def test_widening_reapproves_narrowing_applies(db, ctx, approvals_emitted):
    out = await _request(ctx)
    route_id = out["route_id"]
    await _on_approved(ctx, {"id": "ap1", "status": "approved",
                             "reviewed_by": "owner",
                             "proposal_data": json.dumps(
                                 {"route_id": route_id})})
    assert (await StimulusRepository(ctx.db).get_route(route_id))["enabled"] == 1

    # narrowing: tighter cooldown, same everything else → applied, no approval
    out2 = await _request(ctx, route_id=route_id, cooldown_s=3600)
    assert out2["status"] == "applied (valve tightening)"
    assert (await StimulusRepository(ctx.db).get_route(route_id))["enabled"] == 1
    assert len(approvals_emitted) == 1

    # widening: charter change → route disabled + new approval
    out3 = await _request(ctx, route_id=route_id,
                          charter=CHARTER + " Also chime at noon.")
    assert out3["status"] == "pending approval"
    assert (await StimulusRepository(ctx.db).get_route(route_id))["enabled"] == 0
    assert len(approvals_emitted) == 2


async def test_pending_dedupe_blocks_spam(db, ctx, approvals_emitted):
    assert (await _request(ctx))["ok"]
    await _seed_pending_approval(ctx.db, "arrival-herald")
    out = await _request(ctx)
    assert out["ok"] is False and "pending" in out["error"]


async def test_list_tool(db, ctx, approvals_emitted):
    await _request(ctx)
    await _seed_pending_approval(ctx.db, "arrival-herald")
    tools = _tools(ctx)
    out = json.loads(await tools["list_sensation_routes"].handler())
    assert out["ok"] and len(out["utilities"]) == 1
    u = out["utilities"][0]
    assert u["name"] == "arrival-herald"
    assert u["approval_pending"] is True
    assert u["routes"][0]["pattern"] == "frigate / activity.person / action"
