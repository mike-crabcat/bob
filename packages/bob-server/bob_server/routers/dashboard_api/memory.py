"""Dashboard API: Memory wiki: stats, searches, entities, claims, etc.."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/memory/stats")
async def get_memory_stats(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir
    db = _db(request)

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    # Build stats from database
    type_rows = await db.fetch_all(
        "SELECT entity_type, COUNT(*) AS count FROM memory_entities GROUP BY entity_type"
    )
    categories = {r["entity_type"]: r["count"] for r in type_rows}
    total_entries = sum(categories.values())

    # Recent entries
    recent_rows = await db.fetch_all(
        "SELECT e.entity_id, e.entity_type, e.display_name, e.updated_at, "
        " (SELECT COUNT(*) FROM memory_claims c WHERE c.subject_id = e.entity_id AND c.status = 'active') AS claim_count "
        "FROM memory_entities e ORDER BY e.updated_at DESC LIMIT 50"
    )
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

    # Pipeline status
    bulletins = await svc.read_bulletins(workspace, skip_digested=True)
    pending_bulletins = len(bulletins)

    last_dream = await db.fetch_one(
        "SELECT created_at FROM memory_dream_log ORDER BY created_at DESC LIMIT 1"
    )

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
        "pending_bulletins": pending_bulletins,
        "last_dream": _utc(last_dream["created_at"]) if last_dream else None,
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
        rows = await db.fetch_all(
            "SELECT id, query, results_json, session_key, result_count, latency_seconds, created_at "
            "FROM memory_search_log ORDER BY created_at DESC LIMIT 100"
        )
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

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    import time
    start = time.monotonic()
    result = await svc.search_entries(workspace, query)
    latency = time.monotonic() - start

    # Log it
    from uuid import uuid4
    try:
        await db.execute(
            "INSERT INTO memory_search_log (id, query, results_json, session_key, result_count, latency_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid4()), query, json.dumps(result), None, len(result.get("results", [])), latency),
        )
    except Exception:
        pass

    result["latency_seconds"] = latency
    return result


@router.get("/api/memory/bulletins")
async def get_memory_bulletins(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=_db(request))
    svc = MemoryService(ctx)
    bulletins = await svc.read_bulletins(workspace, skip_digested=True)
    result = []
    for b in bulletins:
        result.append({
            "slug": b.id,
            "source_session": b.source_id,
            "source_type": b.source_type,
            "channel_id": b.channel_id,
            "content": b.content,
            "created_at": b.created_at.timestamp() if hasattr(b.created_at, "timestamp") else 0,
        })
    return {"bulletins": result}


@router.get("/api/memory/bulletins/{bulletin_id}")
async def get_memory_bulletin_detail(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    bulletin_id = request.path_params["bulletin_id"]

    row = await db.fetch_one(
        "SELECT id, created_at, channel_id, source_type, source_id, visibility, content, "
        "digested, session_range_start, session_range_end FROM memory_bulletins WHERE id = ?",
        (bulletin_id,),
    )
    if not row:
        return {"error": "not found"}

    # Find claims that reference this bulletin
    claim_rows = await db.fetch_all(
        "SELECT id, claim_type_key, subject_id, object_id, value, status, visibility, created_at "
        "FROM memory_claims WHERE source_bulletins LIKE ?",
        (f'%"{bulletin_id}"%',),
    )
    claims = [
        {
            "id": r["id"],
            "claim_type_key": r["claim_type_key"],
            "subject_id": r["subject_id"],
            "object_id": r["object_id"],
            "value": r["value"],
            "status": r["status"],
            "visibility": r["visibility"],
            "created_at": r["created_at"],
        }
        for r in claim_rows
    ]

    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "channel_id": row["channel_id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "visibility": row["visibility"],
        "content": row["content"],
        "digested": bool(row["digested"]),
        "session_range_start": row["session_range_start"],
        "session_range_end": row["session_range_end"],
        "claims": claims,
    }


@router.get("/api/memory/dreams")
async def get_memory_dreams(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    dreams: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_dream_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            "SELECT id, bulletins_processed, entries_created, bulletin_slugs, "
            "operations_json, raw_response, duration_seconds, status, created_at "
            "FROM memory_dream_log ORDER BY created_at DESC LIMIT 20"
        )
        for row in rows:
            operations = []
            try:
                parsed = json.loads(row["operations_json"]) if row["operations_json"] else []
                if isinstance(parsed, dict):
                    # Legacy format: just claims count
                    operations = []
                elif isinstance(parsed, list):
                    operations = parsed
            except (json.JSONDecodeError, TypeError):
                pass
            slugs = []
            try:
                slugs = json.loads(row["bulletin_slugs"]) if row["bulletin_slugs"] else []
            except (json.JSONDecodeError, TypeError):
                pass
            claims_extracted = 0
            if isinstance(operations, list):
                claims_extracted = sum(
                    len(op["claims"]) if isinstance(op.get("claims"), list) else op.get("claims", 0)
                    for op in operations
                )
            dreams.append({
                "id": row["id"],
                "bulletins_processed": row["bulletins_processed"],
                "entries_created": row["entries_created"],
                "claims_extracted": claims_extracted,
                "bulletin_slugs": slugs,
                "operations": operations,
                "raw_response": row["raw_response"] or "",
                "duration_seconds": row["duration_seconds"],
                "status": row["status"],
                "created_at": _utc(row["created_at"]),
            })
    return {"dreams": dreams}


@router.get("/api/memory/category/{category}")
async def get_memory_category(request: Request, category: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

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

    query = (
        "SELECT e.entity_id, e.entity_type, e.display_name, e.status, e.updated_at, "
        "(SELECT COUNT(*) FROM memory_claims c WHERE c.subject_id = e.entity_id AND c.status = 'active') as claim_count "
        "FROM memory_entities e"
    )
    params: list[str] = []
    if entity_type:
        query += " WHERE e.entity_type = ?"
        params.append(entity_type)
    query += " ORDER BY e.updated_at DESC"

    rows = await db.fetch_all(query, tuple(params))

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
        placeholders = ",".join("?" for _ in entity_ids)
        claim_rows = await db.fetch_all(
            f"SELECT subject_id, claim_type_key, value, object_id FROM memory_claims "
            f"WHERE subject_id IN ({placeholders}) AND status = 'active'",
            tuple(entity_ids),
        )
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

    from bob_server.context import AppContext
    from bob_server.services.memory.service import MemoryService
    from bob_server.services.memory.claim_service import get_active_claims

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)
    entity = await svc.read_entity(settings.harness.workspace_dir, entity_id)

    if not entity:
        return {"error": "not found"}

    claims = await get_active_claims(db, entity_id)

    from bob_server.services.memory.claim_types import render_entity

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
                "source_bulletins": c.source_bulletins,
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
    rows = await db.fetch_all(
        "SELECT * FROM memory_questions WHERE status = ? ORDER BY created_at DESC LIMIT 100",
        (status_filter,),
    )
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

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    svc = MemoryService(ctx)
    workspace = request.app.state.settings.harness.workspace_dir
    return await svc.answer_question(workspace, question_id, answer)


@router.post("/api/memory/questions/{question_id}/dismiss")
async def dismiss_memory_question(request: Request, question_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    svc = MemoryService(ctx)
    return await svc.dismiss_question(question_id)


@router.get("/api/memory/claims")
async def get_memory_claims(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    conditions: list[str] = []
    params: list[str] = []

    claim_type = request.query_params.get("type", "").strip()
    if claim_type:
        conditions.append("claim_type_key = ?")
        params.append(claim_type)
    subject_id = request.query_params.get("subject_id", "").strip()
    if subject_id:
        conditions.append("subject_id = ?")
        params.append(subject_id)
    status = request.query_params.get("status", "").strip()
    if status:
        conditions.append("status = ?")
        params.append(status)

    query = "SELECT * FROM memory_claims"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT 200"

    rows = await db.fetch_all(query, tuple(params))
    claims = [
        {
            "id": r["id"],
            "claim_type_key": r["claim_type_key"],
            "subject_id": r["subject_id"],
            "object_id": r["object_id"],
            "value": r["value"],
            "status": r["status"],
            "source_bulletins": json.loads(r["source_bulletins"]) if r["source_bulletins"] else [],
            "visibility": r["visibility"],
            "created_at": _utc(r["created_at"]),
        }
        for r in rows
    ]
    return {"claims": claims}


@router.post("/api/memory/digested")
async def get_digested_bulletins(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    slugs: list[str] = body.get("slugs", [])
    if not slugs:
        return {"bulletins": []}

    db = _db(request)
    placeholders = ",".join("?" * len(slugs))
    rows = await db.fetch_all(
        f"SELECT id, content FROM memory_bulletins WHERE id IN ({placeholders}) AND digested = 1",
        tuple(slugs),
    )
    return {"bulletins": [{"slug": r["id"], "content": r["content"]} for r in rows]}


@router.post("/api/memory/redigest")
async def redigest_bulletin(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    # In v6, bulletins are immutable — redigest re-processes a bulletin through the dream pipeline
    body = await request.json()
    slug: str = body.get("slug", "")
    if not slug:
        return {"error": "missing slug"}

    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=_db(request))
    svc = MemoryService(ctx)

    bulletin = await svc.read_bulletin(workspace, slug)
    if not bulletin:
        return {"error": f"bulletin not found: {slug}"}

    result = await svc.process_bulletin(workspace, bulletin)
    return {"ok": True, "slug": slug, "result": result}


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
    for eid in (canonical_id, loser_id):
        row = await db.fetch_one(
            "SELECT entity_id FROM memory_entities WHERE entity_id = ? AND status = 'active'",
            (eid,),
        )
        if not row:
            return {"error": f"entity not found: {eid}"}

    from bob_server.services.memory.merge import _execute_merge
    result = await _execute_merge(db, canonical_id, loser_id)

    # Rebuild FTS + embedding for canonical
    settings = request.app.state.settings
    from bob_server.context import AppContext
    from bob_server.services.memory import MemoryService
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


