"""Stimulus spine tests — ingest endpoint, repository, router drain.

Covers the contract in docs/stimulus-spine-plan.md: dedicated-token auth,
envelope validation, dedup idempotency, TTL expiry, route matching, batching
per target, log-only levels, and the inert-seed rollout invariant (the
migrated route ships disabled).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.repositories.stimulus import StimulusRepository
from server.services.stimulus_router import drain, match_route, render_instruction

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "server" / "schemas"

ENVELOPE = {
    "source": "cryptobro", "type": "signal.momentum", "level": "action",
    "dedup_key": "mom1h:SOL:2026-09-04", "ttl_s": 600,
    "target_hint": "crypto", "summary": "SOL +3.2% over 1h",
    "body": {"asset": "SOL", "chg_1h": 3.2},
}


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


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


# ─── migration ────────────────────────────────────────────────────────

async def test_migration_seeds_route_disabled(db):
    routes = await StimulusRepository(db).routes(enabled_only=False)
    seeded = [r for r in routes if r["source"] == "cryptobro"]
    assert len(seeded) == 1
    assert seeded[0]["enabled"] == 0  # inert rollout: phase-3 flips this
    assert seeded[0]["target_session"].endswith("120363410716086644")


# ─── repository ───────────────────────────────────────────────────────

async def test_insert_dedup_is_idempotent(db):
    repo = StimulusRepository(db)
    kw = dict(source="cryptobro", type_="signal.momentum", level="action",
              ts=datetime.now(timezone.utc).isoformat(),
              dedup_key="k1", ttl_s=600, target_hint=None,
              summary="s", body={})
    id1, inserted1 = await repo.insert_event(**kw)
    id2, inserted2 = await repo.insert_event(**kw)
    assert inserted1 and not inserted2
    assert id1 == id2
    assert len(await repo.pending_events()) == 1


# ─── endpoint ─────────────────────────────────────────────────────────

def test_endpoint_503_when_token_unset(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c",
                        db_path=tmp_path / "d" / "bob.db", stimulus_token="")
    with make_client(settings) as client:
        assert client.post("/api/v1/stimulus/events", json={}).status_code == 503


def test_endpoint_rejects_bad_token(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c",
                        db_path=tmp_path / "d" / "bob.db",
                        stimulus_token="sekrit")
    with make_client(settings) as client:
        r = client.post("/api/v1/stimulus/events", json=ENVELOPE,
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        # no token at all — also 401 (endpoint owns its auth; the middleware
        # exempts the path because the token is dedicated, not absent)
        assert client.post("/api/v1/stimulus/events", json=ENVELOPE).status_code == 401


def test_endpoint_accepts_dedups_and_validates(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c",
                        db_path=tmp_path / "d" / "bob.db",
                        stimulus_token="sekrit")
    with make_client(settings) as client:
        r1 = client.post("/api/v1/stimulus/events", json=ENVELOPE,
                         headers={"Authorization": "Bearer sekrit"})
        assert r1.status_code == 200 and r1.json()["id"] >= 1
        r2 = client.post("/api/v1/stimulus/events", json=ENVELOPE,
                         headers={"Authorization": "Bearer sekrit"})
        assert r2.status_code == 200 and r2.json()["duplicate"] is True

        for bad in [{"type": "x"}, {"source": ""}, {"level": "urgent"},
                    {"source": "s", "type": "t", "ttl_s": 0},
                    {"source": "s", "type": "t", "summary": 5},
                    {"source": "s", "type": "t", "body": [1]}]:
            r = client.post("/api/v1/stimulus/events", json=bad,
                            headers={"Authorization": "Bearer sekrit"})
            assert r.status_code == 400, bad


# ─── router drain ─────────────────────────────────────────────────────

async def _seed_event(db, **over):
    kw = dict(source="cryptobro", type_="signal.momentum", level="action",
              ts=datetime.now(timezone.utc).isoformat(),
              dedup_key=None, ttl_s=600, target_hint=None,
              summary="SOL +3.2%", body={"asset": "SOL"})
    kw.update(over)
    if kw["dedup_key"] is None:
        kw["dedup_key"] = f"k-{over.get('source', 'cryptobro')}-{over.get('type_', 't')}-{len(str(kw))}"
    return await StimulusRepository(db).insert_event(**kw)


async def test_drain_batches_per_target_and_marks_processed(db, ctx, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_wake(ctx, session_key, content, **kw):
        calls.append((session_key, content))
        return True

    import server.services.wake_service as ws
    monkeypatch.setattr(ws, "wake_conversation", fake_wake)

    await db.execute(
        "UPDATE stimulus_routes SET enabled = 1 WHERE source = 'cryptobro'")
    await _seed_event(db, dedup_key="a1")
    await _seed_event(db, dedup_key="a2")  # same route, same tick -> one steer

    counts = await drain(ctx)
    assert counts["steered"] == 2
    assert len(calls) == 1  # batched: correlated events wake ONE turn
    assert "120363410716086644" in calls[0][0]
    assert "no reply is needed" in calls[0][1]  # silent-decline line
    assert calls[0][1].count("[Stimulus:") == 2
    assert (await StimulusRepository(db).pending_events()) == []


async def test_drain_expires_stale_and_logs_info(db, ctx):
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    await _seed_event(db, dedup_key="stale", ts=stale_ts, ttl_s=600)
    await _seed_event(db, dedup_key="info", level="info")

    counts = await drain(ctx)
    assert counts["expired"] == 1 and counts["logged"] == 1 and counts["steered"] == 0
    rows = {r["dedup_key"]: r for r in await db.fetch_all(
        "SELECT dedup_key, delivered_steer FROM stimulus_events")}
    assert rows["stale"]["delivered_steer"] == "expired"
    assert rows["info"]["delivered_steer"] == "log-only"


async def test_drain_unrouted_action_is_log_only(db, ctx):
    await _seed_event(db, dedup_key="u1", source="radio", type_="on_air")
    counts = await drain(ctx)
    assert counts["steered"] == 0 and counts["logged"] == 1


def test_match_route_glob_and_priority():
    routes = [
        {"source": "*", "type_pattern": "signal.*", "level": "*",
         "target_session": "generic", "priority": 10},
        {"source": "cryptobro", "type_pattern": "signal.*", "level": "action",
         "target_session": "crypto", "priority": 0},
    ]
    hit = match_route({"source": "cryptobro", "type": "signal.momentum",
                       "level": "action"}, routes)
    assert hit["target_session"] == "crypto"  # priority 0 beats wildcard 10
    miss = match_route({"source": "cryptobro", "type": "node.down",
                        "level": "action"}, routes)
    assert miss is None


def test_render_instruction_carries_summary_and_dedup():
    text = render_instruction([{**ENVELOPE}])
    assert "SOL +3.2% over 1h" in text
    assert "mom1h:SOL:2026-09-04" in text
    assert "no reply is needed" in text
