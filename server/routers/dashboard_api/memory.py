"""Dashboard API: Memory wiki: stats, searches, entities, claims, etc.."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/memory/stats")
async def get_memory_stats(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    # Build stats from database
    from server.services.memory import admin as memory_admin
    categories = await memory_admin.entity_type_counts(db)
    total_entries = sum(categories.values())

    # Recent entries
    recent_rows = await memory_admin.recent_entities(db, limit=50)
    recent = []
    for r in recent_rows:
        recent.append({
            "path": r["entity_id"],
            "wiki": "core",
            "category": r["entity_type"],
            "slug": r["entity_id"],
            "title": r["display_name"] or "",
            "summary": f"{r['claim_count']} claims",
            "modified": r["updated_at"],
        })

    return {
        "stats": {
            "total_entries": total_entries,
            "wikis": {
                "core": {
                    "entries": total_entries,
                    "categories": categories,
                },
            },
        },
        "recent": recent[:50],
    }


@router.get("/api/memory/searches")
async def get_memory_searches(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    searches: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_search_log'"
    )
    if table_exists:
        from server.services.memory import admin as memory_admin
        rows = await memory_admin.list_search_log(db, limit=100)
        for row in rows:
            results = []
            abstract = ""
            try:
                parsed = json.loads(row["results_json"]) if row["results_json"] else {}
                if isinstance(parsed, dict):
                    results = parsed.get("results", [])
                    abstract = parsed.get("abstract", "")
                elif isinstance(parsed, list):
                    results = parsed
            except (json.JSONDecodeError, TypeError):
                pass
            searches.append({
                "id": row["id"],
                "query": row["query"],
                "abstract": abstract,
                "results": results,
                "session_key": row["session_key"],
                "result_count": row["result_count"],
                "latency_seconds": row["latency_seconds"],
                "created_at": _utc(row["created_at"]),
            })
    return {"searches": searches}


@router.get("/api/memory/search")
async def run_memory_search(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    query = request.query_params.get("q", "").strip()
    if not query:
        return {"error": "missing query parameter 'q'"}

    db = _db(request)
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from server.context import AppContext
    from server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    import time
    start = time.monotonic()
    result = await svc.search_entries(workspace, query)
    latency = time.monotonic() - start

    # Log it
    from uuid import uuid4
    try:
        from server.services.memory import admin as memory_admin
        await memory_admin.insert_search_log(
            db, log_id=str(uuid4()), query=query, results_json=json.dumps(result),
            session_key=None, result_count=len(result.get("results", [])),
            latency_seconds=latency)
    except Exception:
        pass

    result["latency_seconds"] = latency
    return result


@router.get("/api/memory/category/{category}")
async def get_memory_category(request: Request, category: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from server.context import AppContext
    from server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=_db(request))
    svc = MemoryService(ctx)
    entries = await svc.browse_category(workspace, "core", category)
    for e in entries:
        e["path"] = f"memory/entities/{category}/{e['slug']}.md"
    return {"category": category, "entries": entries}


@router.get("/api/memory/entities")
async def get_memory_entities(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    entity_type = request.query_params.get("type", "").strip()

    from server.services.memory import admin as memory_admin
    rows = await memory_admin.list_entities(db, entity_type=entity_type)

    # Build a summary per entity from key claims
    summary_keys = {
        "file": "file_path",
        "thing": "thing_type",
        "task": "task_status",
        "location": "location_type",
        "transport": "transport_type",
        "trip": "destination",
        "decision": "rationale",
        "event": "location",
        "stay": "accommodation",
    }

    entity_ids = [r["entity_id"] for r in rows]
    summaries: dict[str, str] = {}
    if entity_ids:
        claim_rows = await memory_admin.claims_for_subjects(db, entity_ids)
        for cr in claim_rows:
            eid = cr["subject_id"]
            if eid in summaries:
                continue
            etype = next((r["entity_type"] for r in rows if r["entity_id"] == eid), "")
            key = summary_keys.get(etype, "")
            if key and cr["claim_type_key"] == key:
                summaries[eid] = cr["value"] or cr["object_id"] or ""

    entities = [
        {
            "entity_id": r["entity_id"],
            "entity_type": r["entity_type"],
            "display_name": r["display_name"] or "",
            "status": r["status"] or "active",
            "updated_at": _utc(r["updated_at"]),
            "claim_count": r["claim_count"],
            "summary": summaries.get(r["entity_id"], ""),
        }
        for r in rows
    ]
    return {"entities": entities}


@router.get("/api/memory/entities/{entity_id:path}")
async def get_memory_entity_detail(request: Request, entity_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    settings = request.app.state.settings

    from server.context import AppContext
    from server.services.memory.service import MemoryService
    from server.services.memory.claim_service import get_active_claims

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)
    entity = await svc.read_entity(settings.harness.workspace_dir, entity_id)

    if not entity:
        return {"error": "not found"}

    claims = await get_active_claims(db, entity_id)

    from server.services.memory.claim_types import render_entity

    claim_dicts = [
        {"claim_type_key": c.claim_type_key, "object_id": c.object_id, "value": c.value}
        for c in claims
    ]
    rendered = await render_entity(entity.entity_type, entity.display_name, claim_dicts, entity_id=entity.entity_id, db=db)

    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "status": entity.status,
        "rendered": rendered,
        "claims": [
            {
                "id": c.id,
                "claim_type_key": c.claim_type_key,
                "subject_id": c.subject_id,
                "object_id": c.object_id,
                "value": c.value,
                "status": c.status,
                "visibility": c.visibility,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in claims
        ],
    }


@router.get("/api/memory/questions")
async def get_memory_questions(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    status_filter = request.query_params.get("status", "open").strip()
    from server.services.memory import admin as memory_admin
    rows = await memory_admin.list_questions(db, status=status_filter, limit=100)
    questions = [
        {
            "id": r["id"],
            "entity_id": r["entity_id"],
            "question": r["question"],
            "options": json.loads(r["options"]) if r["options"] else [],
            "context": r["context"] or "",
            "status": r["status"],
            "answer": r["answer"],
            "created_at": _utc(r["created_at"]),
            "answered_at": _utc(r["answered_at"]) if r["answered_at"] else None,
        }
        for r in rows
    ]
    return {"questions": questions}


@router.post("/api/memory/questions/{question_id}/answer")
async def answer_memory_question(request: Request, question_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    body = await request.json()
    answer = body.get("answer", "").strip()
    if not answer:
        return {"error": "answer is required"}

    from server.context import AppContext
    from server.services.memory import MemoryService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    svc = MemoryService(ctx)
    workspace = request.app.state.settings.harness.workspace_dir
    return await svc.answer_question(workspace, question_id, answer)


@router.post("/api/memory/questions/{question_id}/dismiss")
async def dismiss_memory_question(request: Request, question_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from server.context import AppContext
    from server.services.memory import MemoryService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    svc = MemoryService(ctx)
    return await svc.dismiss_question(question_id)


@router.get("/api/memory/claims")
async def get_memory_claims(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from server.services.memory import admin as memory_admin
    rows = await memory_admin.list_claims(
        db,
        claim_type=request.query_params.get("type", "").strip(),
        subject_id=request.query_params.get("subject_id", "").strip(),
        status=request.query_params.get("status", "").strip(),
        limit=200)
    claims = [
        {
            "id": r["id"],
            "claim_type_key": r["claim_type_key"],
            "subject_id": r["subject_id"],
            "object_id": r["object_id"],
            "value": r["value"],
            "status": r["status"],
            "visibility": r["visibility"],
            "created_at": _utc(r["created_at"]),
        }
        for r in rows
    ]
    return {"claims": claims}


@router.post("/api/memory/entities/merge")
@router.post("/api/memory/entities/merge")
async def merge_entities(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    canonical_id: str = body.get("canonical_id", "")
    loser_id: str = body.get("loser_id", "")
    if not canonical_id or not loser_id:
        return {"error": "missing canonical_id or loser_id"}

    db = _db(request)

    # Verify both entities exist
    from server.services.memory import admin as memory_admin
    for eid in (canonical_id, loser_id):
        row = await memory_admin.get_active_entity(db, eid)
        if not row:
            return {"error": f"entity not found: {eid}"}

    from server.services.memory.merge import _execute_merge
    result = await _execute_merge(db, canonical_id, loser_id)

    # Rebuild FTS + embedding for canonical
    settings = request.app.state.settings
    from server.context import AppContext
    from server.services.memory import MemoryService
    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)
    await svc._update_entity_fts(canonical_id)

    return {"ok": True, **result}


@router.post("/api/memory/backfill-people")
async def backfill_people(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    # In v6, people are populated through the seed process, not backfilled
    return {"ok": True, "message": "Use 'bob memory seed' to regenerate from session history"}




@router.get("/api/memory/routing-log")
async def routing_log(request: Request) -> dict[str, Any]:
    """Recent claim-routing decisions (Bob Events §4.2) — the routing
    analogue of the attention shadow table."""
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from server.services.memory.claim_router import recent_routing_log

    rows = await recent_routing_log(_db(request), limit=50)
    import json as _json
    for r in rows:
        for col in ("claim_ids", "entity_ids"):
            try:
                r[col] = _json.loads(r.get(col) or "[]")
            except (TypeError, ValueError):
                r[col] = []
    return {"decisions": rows}
