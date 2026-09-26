"""OpenRouter routing constraint + serving attribution (2026-09-26).

glm-5.3-flash rides a 33-endpoint OpenRouter pool that includes fp4/nvfp4
and undeclared-quant hosts (the fp4 ones are the cheapest, so price-weighted
routing lands on them often). Two changes are pinned here:
- provider.quantizations allowlist injected on every request shape
  (_routing_extra), so low-precision endpoints are excluded while
  price/latency competition continues among allowed-precision hosts;
- generation_id captured per call and resolved to served_by/served_quant
  by the heartbeat attribution sweep (the Responses API returns the gen id
  in-band but not the serving provider).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "server" / "schemas"

OR_MODEL = "z-ai/glm-5.3-flash"
OPENAI_MODEL = "gpt-5.6-x"


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
    from server.config import Settings
    from server.context import AppContext
    return AppContext(db=db, settings=Settings(
        data_dir=tmp_path / "d", config_dir=tmp_path / "c",
        db_path=tmp_path / "d" / "bob.db"))


# ------------------------------------------------------------ routing extra

def _settings(**or_overrides):
    from server.config import Settings, OpenRouterSettings
    s = Settings(data_dir=Path("/tmp/x"), config_dir=Path("/tmp/x"))
    object.__setattr__(s, "openrouter",
                       OpenRouterSettings(api_key="k", **or_overrides))
    return s


def test_routing_extra_defaults_to_fp8_for_openrouter_models():
    from server.services.openai_service import _routing_extra
    extra = _routing_extra(_settings(), OR_MODEL)
    assert extra == {"extra_body": {"provider": {"quantizations": ["fp8"]}}}


def test_routing_extra_respects_multi_and_disabled():
    from server.services.openai_service import _routing_extra
    assert _routing_extra(
        _settings(quantizations="fp8, bf16"), OR_MODEL
    ) == {"extra_body": {"provider": {"quantizations": ["fp8", "bf16"]}}}
    assert _routing_extra(_settings(quantizations=""), OR_MODEL) == {}
    assert _routing_extra(_settings(quantizations="off"), OR_MODEL) == {}
    assert _routing_extra(_settings(quantizations="  "), OR_MODEL) == {}


def test_routing_extra_skips_non_openrouter_models():
    from server.services.openai_service import _routing_extra
    # Direct-OpenAI models never carry routing params, whatever the config.
    assert _routing_extra(_settings(), OPENAI_MODEL) == {}


def test_note_generation_keeps_last_id():
    from server.services.openai_service import _note_generation

    class _Resp:
        def __init__(self, id_):
            self.id = id_

    meta: dict = {}
    _note_generation(meta, _Resp("gen-1"))
    _note_generation(meta, _Resp("gen-2"))
    assert meta == {"generation_id": "gen-2"}
    empty: dict = {}
    _note_generation(empty, _Resp(None))
    _note_generation(None, _Resp("gen-x"))  # no out-param: no-op
    assert empty == {}


# ------------------------------------------------------- repo: attribution

async def test_upsert_carries_generation_id_through_update(db):
    from server.repositories.llm_call_log import LlmCallLogRepository
    repo = LlmCallLogRepository(db)
    log_id = await repo.upsert(
        provider="openrouter", model=OR_MODEL, call_category="quick_prompt",
        status="running")
    # Completion update carries the generation id; a later no-id update
    # must not wipe it (COALESCE).
    await repo.upsert(log_id=log_id, status="completed",
                      response_text="ok", generation_id="gen-abc")
    await repo.upsert(log_id=log_id, tool_blocks_json="[]")
    row = await repo.get(log_id)
    assert row["generation_id"] == "gen-abc"


async def test_unattributed_queue_and_set_served(db):
    from server.repositories.llm_call_log import LlmCallLogRepository
    repo = LlmCallLogRepository(db)
    a = await repo.upsert(provider="openrouter", model=OR_MODEL,
                          generation_id="gen-a")
    await repo.upsert(provider="openrouter", model=OR_MODEL,
                      generation_id="gen-b")
    # No gen id → never queued; already served → never re-queued.
    await repo.upsert(provider="openrouter", model=OR_MODEL)
    c = await repo.upsert(provider="openrouter", model=OR_MODEL,
                          generation_id="gen-c")
    await repo.set_served(c, served_by="Z.AI", served_quant="fp8")

    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc)
             - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    queued = await repo.unattributed_generations(since_iso=since)
    ids = {r["generation_id"] for r in queued}
    assert ids == {"gen-a", "gen-b"}

    await repo.set_served(a, served_by="Sail Research", served_quant=None)
    remaining = await repo.unattributed_generations(since_iso=since)
    assert [r["generation_id"] for r in remaining] == ["gen-b"]


# --------------------------------------------------------- attribution task

class _FakeGenResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeClient:
    """Stands in for httpx.AsyncClient: serves canned generation lookups."""
    responses: dict[str, _FakeGenResponse] = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, params=None):
        return self.responses.get(
            params.get("id", ""),
            _FakeGenResponse(200, {"data": {"provider_name": "Z.AI",
                                            "quantization": "fp8"}}))


async def test_attribution_task_resolves_and_marks_gone(ctx, db, monkeypatch):
    from server.repositories.llm_call_log import LlmCallLogRepository
    repo = LlmCallLogRepository(db)
    live = await repo.upsert(provider="openrouter", model=OR_MODEL,
                             generation_id="gen-live")
    old = await repo.upsert(provider="openrouter", model=OR_MODEL,
                            generation_id="gen-old")
    fresh404 = await repo.upsert(provider="openrouter", model=OR_MODEL,
                                 generation_id="gen-fresh404")
    # Only the "old" row is past the 1h lag grace: backdate it.
    from datetime import datetime, timezone, timedelta
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("UPDATE llm_call_log SET created_at = ? WHERE id = ?",
                     (stale, old))
    _FakeClient.responses = {
        "gen-live": _FakeGenResponse(200, {"data": {
            "provider_name": "Sail Research", "quantization": None}}),
        "gen-old": _FakeGenResponse(404, {}),
        "gen-fresh404": _FakeGenResponse(404, {}),
    }
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.delenv("BOB_OPENROUTER_ATTRIBUTION", raising=False)
    object.__setattr__(ctx.settings, "openrouter",
                       __import__("server.config", fromlist=["OpenRouterSettings"])
                       .OpenRouterSettings(api_key="k"))

    from server.heartbeat import OpenRouterAttributionTask
    await OpenRouterAttributionTask().run(ctx)

    live_row = await repo.get(live)
    assert live_row["served_by"] == "Sail Research"
    assert live_row["served_quant"] is None
    old_row = await repo.get(old)
    assert old_row["served_by"] == "(gone)"  # past grace: not retried forever
    # A FRESH 404 is indexing lag (ids resolve ~90s after the call, verified
    # live 2026-09-26) — stays queued for the next sweep, not marked gone.
    fresh_row = await repo.get(fresh404)
    assert fresh_row["served_by"] is None
    since = (datetime.now(timezone.utc)
             - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert [r["generation_id"] for r in
            await repo.unattributed_generations(since_iso=since)] == ["gen-fresh404"]


async def test_attribution_task_kill_switch(ctx, db, monkeypatch):
    from server.repositories.llm_call_log import LlmCallLogRepository
    repo = LlmCallLogRepository(db)
    await repo.upsert(provider="openrouter", model=OR_MODEL,
                      generation_id="gen-never")
    monkeypatch.setenv("BOB_OPENROUTER_ATTRIBUTION", "off")
    object.__setattr__(ctx.settings, "openrouter",
                       __import__("server.config", fromlist=["OpenRouterSettings"])
                       .OpenRouterSettings(api_key="k"))

    from server.heartbeat import OpenRouterAttributionTask
    await OpenRouterAttributionTask().run(ctx)

    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert len(await repo.unattributed_generations(since_iso=since)) == 1
