"""Fixes from the 2026-09-12 memory review (docs/memory-review-2026-09.md).

Covers the GLM JSON-rescue ladder (llm_json), the reconciliation final-parse
recovery + tolerant tool wrappers, claim-router verdict rescue, recall
search-logging, and embedding input truncation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.services.llm_json import parse_llm_json, parse_llm_verdict

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "server" / "schemas"


# ---------------------------------------------------------------------------
# llm_json — the rescue ladder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"issues": []}\n```', {"issues": []}),
    ('Here is the result: {"a": [1, 2]} hope that helps', {"a": [1, 2]}),
    # truncation mid-string: closed at the last complete token
    ('{"issues": [{"summary": "x", "fix": "supersede cla',
     {"issues": [{"summary": "x", "fix": None}]}),
    # truncation mid-array element
    ('{"questions": [{"q": "who?"}, {"q": "wh',
     {"questions": [{"q": "who?"}, {"q": None}]}),
    # dangling separators
    ('{"a": 1,', {"a": None}),
    ('{"a":', {"a": None}),
])
def test_parse_llm_json_recovers(text, expected):
    assert parse_llm_json(text) == expected


@pytest.mark.parametrize("text", ["", "Done.", "no braces here", None, "```"])
def test_parse_llm_json_gives_up(text):
    assert parse_llm_json(text) is None


def test_parse_llm_verdict_json():
    assert parse_llm_verdict('{"verdict": "IGNORE"}',
                             {"RELEVANT": "relevant", "IGNORE": "ignore"}) == "ignore"


def test_parse_llm_verdict_truncated_json():
    assert parse_llm_verdict('{"verdict": "RELEV',
                             {"RELEVANT": "relevant", "IGNORE": "ignore"}) == "relevant"


def test_parse_llm_verdict_prose_fallback():
    assert parse_llm_verdict("The facts are clearly RELEVANT to this plan.",
                             {"RELEVANT": "relevant", "IGNORE": "ignore"}) == "relevant"


def test_parse_llm_verdict_absent():
    assert parse_llm_verdict("no idea",
                             {"RELEVANT": "relevant", "IGNORE": "ignore"}) is None


# ---------------------------------------------------------------------------
# reconciliation — tolerant tool-call kwargs
# ---------------------------------------------------------------------------

def test_normalise_tool_kwargs_renames_aliases():
    from server.services.memory.reconciliation import _normalise_tool_kwargs

    async def supersede_claim_tool(subject_id: str, claim_type_key: str = "",
                                   old_value: str = "", new_value: str = "",
                                   new_object_id: str = "") -> str:
        return "ok"

    kwargs, dropped = _normalise_tool_kwargs(
        "supersede_claim_tool", supersede_claim_tool,
        {"subject_id": "person-x", "claim_type": "status",
         "old": "in-progress", "new": "done", "bogus": 1})
    assert kwargs == {"subject_id": "person-x", "claim_type_key": "status",
                      "old_value": "in-progress", "new_value": "done"}
    assert dropped == ["bogus"]


def test_normalise_tool_kwargs_clean_pass_through():
    from server.services.memory.reconciliation import _normalise_tool_kwargs

    async def retract_claim(subject_id: str, claim_type_key: str = "",
                            old_value: str = "") -> str:
        return "ok"

    kwargs, dropped = _normalise_tool_kwargs(
        "retract_claim", retract_claim,
        {"subject_id": "person-x", "claim_type_key": "status"})
    assert kwargs == {"subject_id": "person-x", "claim_type_key": "status"}
    assert dropped == []


# ---------------------------------------------------------------------------
# reconciliation — final parse + wrapper behaviour against a scripted LLM
# ---------------------------------------------------------------------------

class _ScriptedLLM:
    """Stands in for LLMDispatchService inside reconcile_entity."""

    memory_model = "test-model"

    def __init__(self, final_response: str, script=None, *retry_responses: str):
        self._finals = [final_response, *retry_responses]
        self._script = script or []
        self._calls = 0

    _calls = 0

    async def chat_with_tools(self, *, tools, **kwargs):
        if self._calls == 0:
            for step in self._script:
                await step(tools)
        idx = min(self._calls, len(self._finals) - 1)
        self._calls += 1
        return self._finals[idx]


async def _seed_person(db, entity_id="person-review-test"):
    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, created_at) "
        "VALUES (?, 'person', 'Review Test', 'active', datetime('now'))",
        (entity_id,))
    return entity_id


@pytest.fixture
async def recon_db():
    from server.database import Database

    db = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await db.connect()
    await db.apply_migrations()
    yield db
    await db.close()


async def test_reconcile_recovers_truncated_final(recon_db):
    from server.services.memory.reconciliation import reconcile_entity

    eid = await _seed_person(recon_db)
    llm = _ScriptedLLM(
        '{"issues": [{"summary": "dup claims"}, {"summary": "odd date"}], '
        '"questions": [{"question": "which date is right?", "context": "conflict"}, '
        '{"question": "wh')
    result = await reconcile_entity(recon_db, llm, eid)

    assert len(result["issues"]) == 2
    # the truncated second question must be repaired to a stub and then
    # dropped (its question text is None), leaving the good one
    assert len(result["questions_raised"]) == 1
    row = await recon_db.fetch_one(
        "SELECT question, entity_id FROM memory_questions WHERE status='open'")
    assert row["question"] == "which date is right?"
    assert row["entity_id"] == eid


async def test_reconcile_final_garbage_does_not_raise(recon_db):
    from server.services.memory.reconciliation import reconcile_entity

    eid = await _seed_person(recon_db)
    result = await reconcile_entity(recon_db, _ScriptedLLM("Done."), eid)
    assert result["issues"] == []
    assert result["questions_raised"] == []


async def test_reconcile_prose_final_recovered_by_retry(recon_db):
    """First reply in plain prose (no JSON) → one retry turn re-emits the
    JSON summary and the questions survive."""
    from server.services.memory.reconciliation import reconcile_entity

    eid = await _seed_person(recon_db)
    llm = _ScriptedLLM(
        "The entity checks out cleanly. One ambiguity remains about a date.",
        None,
        '{"issues": [], "questions": [{"question": "which date?", "context": "conflict"}]}')
    result = await reconcile_entity(recon_db, llm, eid)

    assert llm._calls == 2
    assert len(result["questions_raised"]) == 1
    row = await recon_db.fetch_one(
        "SELECT question FROM memory_questions WHERE status='open'")
    assert row["question"] == "which date?"


async def _call_tool(tools, name, kwargs):
    tool = next(t for t in tools if t.name == name)
    return await tool.handler(**kwargs)


async def test_reconcile_tool_wrapper_survives_bad_kwargs(recon_db):
    """Renamed + missing arguments must return an error string, not raise."""
    from server.services.memory.reconciliation import reconcile_entity

    eid = await _seed_person(recon_db)
    outcomes = {}

    async def misbehave(tools):
        outcomes["renamed"] = await _call_tool(
            tools, "supersede_claim_tool",
            {"subject": eid, "claim_type": "status", "old": "x", "new": "y"})
        outcomes["missing"] = await _call_tool(
            tools, "retract_claim", {"claim_type_key": "status"})

    llm = _ScriptedLLM('{"issues": [], "questions": []}', script=[misbehave])
    result = await reconcile_entity(recon_db, llm, eid)

    assert outcomes["renamed"].startswith("No matching claim found")
    assert outcomes["missing"].startswith("Error: missing required argument")
    # error results are not counted as applied ops
    assert result["operations_applied"] == []


async def test_reconcile_records_real_ops(recon_db):
    from server.services.memory.reconciliation import reconcile_entity

    eid = await _seed_person(recon_db)

    async def apply_fix(tools):
        await _call_tool(tools, "add_claim",
                         {"subject_id": eid, "claim_type_key": "name",
                          "value": "Review Test"})

    llm = _ScriptedLLM('{"issues": [{"summary": "added name"}], "questions": []}',
                       script=[apply_fix])
    result = await reconcile_entity(recon_db, llm, eid)

    assert len(result["operations_applied"]) == 1
    assert "add_claim" in result["operations_applied"][0]
    row = await recon_db.fetch_one(
        "SELECT COUNT(*) n FROM memory_claims WHERE subject_id = ? AND claim_type_key = 'name'",
        (eid,))
    assert row["n"] == 1


# ---------------------------------------------------------------------------
# recall — search logging
# ---------------------------------------------------------------------------

@pytest.fixture
async def recall_tools_ctx(recon_db):
    from server.config import Settings
    from server.context import AppContext
    from server.services.memory_tools import make_memory_tools

    await _seed_person(recon_db, "person-recall-target")
    ctx = AppContext(db=recon_db, settings=Settings.from_env())
    tools = make_memory_tools(ctx, session_key="agent:main:whatsapp:group:test")
    recall = next(t for t in tools if t.name == "recall")
    return recon_db, recall


async def test_recall_hit_is_logged(recall_tools_ctx):
    db, recall = recall_tools_ctx
    text = await recall.handler(query="person-recall-target")
    assert "Review Test" in text or "person-recall-target" in text

    row = await db.fetch_one(
        "SELECT query, session_key, result_count FROM memory_search_log "
        "ORDER BY created_at DESC")
    assert row["query"] == "person-recall-target"
    assert row["session_key"] == "agent:main:whatsapp:group:test"
    assert row["result_count"] == 1


async def test_recall_miss_is_logged_with_zero(recall_tools_ctx):
    db, recall = recall_tools_ctx
    text = await recall.handler(query="person-does-not-exist")
    assert "No entity found" in text

    row = await db.fetch_one(
        "SELECT result_count FROM memory_search_log ORDER BY created_at DESC")
    assert row["result_count"] == 0


# ---------------------------------------------------------------------------
# embedding — input truncation
# ---------------------------------------------------------------------------

async def test_embed_batch_truncates_oversize(monkeypatch):
    from server.services.memory import embedding

    captured: dict[str, Any] = {}

    class _FakeData:
        vec = [0.05] * embedding.EMBEDDING_DIMS

    class _FakeEmbeddings:
        async def create(self, *, model, input):
            captured["input"] = input
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=_FakeData.vec) for _ in input])

    class _FakeOpenAI:
        def __init__(self, api_key=None):
            self.embeddings = _FakeEmbeddings()

    monkeypatch.setenv("BOB_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)

    result = await embedding.embed_batch(["x" * 100_000, "short"])
    assert captured["input"][0] == "x" * embedding._EMBED_MAX_CHARS
    assert captured["input"][1] == "short"
    assert all(v is not None for v in result)


async def test_embed_batch_no_key_returns_none(monkeypatch):
    from server.services.memory import embedding

    monkeypatch.delenv("BOB_OPENAI_API_KEY", raising=False)
    assert await embedding.embed_batch(["anything"]) == [None]
