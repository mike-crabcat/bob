"""Dashboard API: dream system — runs/journal, resolutions, plans, controls."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


def _ctx(request: Request) -> Any:
    from bob_server.context import AppContext

    return AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
        whatsapp_bridge=getattr(request.app.state, "whatsapp_bridge_service", None),
    )


@router.get("/api/dreams/stats")
async def get_dream_stats(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from bob_server.services.dream import DreamStore
    from bob_server.services.dream import config as dream_config

    store = DreamStore(_ctx(request))
    settings = _ctx(request).settings.dream
    plan_rows = await db.fetch_all("SELECT status, COUNT(*) AS n FROM dream_plans GROUP BY status")
    res_rows = await db.fetch_all("SELECT status, COUNT(*) AS n FROM dream_resolutions GROUP BY status")
    runs = await store.list_runs(1)
    auto = await dream_config.get_auto_approve_plans(db, settings.auto_approve_plans)
    return {
        "enabled": settings.enabled,
        "draft_mode": settings.draft_mode,
        "auto_approve_plans": auto,
        "interval_minutes": settings.interval_minutes,
        "caps": {
            "sessions_per_run": settings.max_sessions_per_run,
            "new_items_per_type": settings.max_new_items_per_type,
            "announce_daily_cap_per_session": settings.announce_daily_cap_per_session,
        },
        "plans_by_status": {r["status"]: r["n"] for r in plan_rows or []},
        "resolutions_by_status": {r["status"]: r["n"] for r in res_rows or []},
        "last_run": runs[0] if runs else None,
    }


@router.get("/api/dreams/runs")
async def list_dream_runs(request: Request, limit: int = 20) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.dream import DreamStore

    runs = await DreamStore(_ctx(request)).list_runs(limit)
    return {"runs": runs}


@router.get("/api/dreams/runs/{run_id}")
async def get_dream_run(request: Request, run_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.dream import DreamStore

    run = await DreamStore(_ctx(request)).get_run(run_id)
    if run is None:
        return {"error": "not_found"}
    return {"run": run}


@router.get("/api/dreams/resolutions")
async def list_dream_resolutions(request: Request, status: str = "") -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.dream import DreamStore

    statuses = [s for s in status.split(",") if s.strip()] or None
    rows = await DreamStore(_ctx(request)).list_resolutions(statuses)
    return {"resolutions": rows}


@router.get("/api/dreams/plans")
async def list_dream_plans(request: Request, status: str = "") -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.dream import DreamStore

    statuses = [s for s in status.split(",") if s.strip()] or None
    rows = await DreamStore(_ctx(request)).list_plans(statuses)
    return {"plans": rows}


@router.post("/api/dreams/plans/{plan_id}/approve")
async def approve_dream_plan(request: Request, plan_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.base import iso_utc
    from bob_server.services.dream import DreamStore
    from bob_server.services.dream.models import Evidence

    store = DreamStore(_ctx(request))
    plan = await store.get_plan(plan_id)
    if plan is None:
        return {"error": "not_found"}
    if plan["status"] not in ("draft", "proposed"):
        return {"error": f"cannot approve from status {plan['status']}"}
    await store.set_plan_status(
        plan_id, "approved", approved_by="operator",
        evidence=Evidence(kind="approved", note="operator approval", at=iso_utc()),
    )
    # Announcement goes out on the next flush (heartbeat sweep) — or now.
    from bob_server.services.dream.announce import AnnounceService

    announce = await AnnounceService(_ctx(request)).flush()
    return {"ok": True, "announce": announce}


@router.post("/api/dreams/plans/{plan_id}/dismiss")
async def dismiss_dream_plan(request: Request, plan_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.base import iso_utc
    from bob_server.services.dream import DreamStore
    from bob_server.services.dream.models import Evidence

    store = DreamStore(_ctx(request))
    plan = await store.get_plan(plan_id)
    if plan is None:
        return {"error": "not_found"}
    if plan["status"] in ("completed", "expired", "dismissed"):
        return {"error": f"already {plan['status']}"}
    await store.set_plan_status(
        plan_id, "dismissed",
        evidence=Evidence(kind="dismissed", note="operator dismissal", at=iso_utc()),
    )
    return {"ok": True}


@router.post("/api/dreams/resolutions/{resolution_id}/promote")
async def promote_dream_resolution(request: Request, resolution_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.base import iso_utc
    from bob_server.services.dream import DreamStore
    from bob_server.services.dream.models import Evidence

    store = DreamStore(_ctx(request))
    row = await db_get_resolution(_db(request), resolution_id)
    if row is None:
        return {"error": "not_found"}
    if row["status"] != "draft":
        return {"error": f"cannot promote from status {row['status']}"}
    await store.set_resolution_status(
        resolution_id, "open",
        evidence=Evidence(kind="promoted", note="operator promotion", at=iso_utc()),
    )
    return {"ok": True}


@router.post("/api/dreams/resolutions/{resolution_id}/drop")
async def drop_dream_resolution(request: Request, resolution_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.base import iso_utc
    from bob_server.services.dream import DreamStore
    from bob_server.services.dream.models import Evidence

    store = DreamStore(_ctx(request))
    row = await db_get_resolution(_db(request), resolution_id)
    if row is None:
        return {"error": "not_found"}
    await store.set_resolution_status(
        resolution_id, "dropped",
        evidence=Evidence(kind="dropped", note="operator drop", at=iso_utc()),
    )
    return {"ok": True}


@router.post("/api/dreams/autoplan")
async def set_autoplan(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    enabled = bool(body.get("enabled"))
    from bob_server.services.dream import config as dream_config

    await dream_config.set_auto_approve_plans(_db(request), enabled)
    return {"ok": True, "auto_approve_plans": enabled}


@router.post("/api/dreams/run")
async def trigger_dream_run(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    import asyncio

    from bob_server.services.dream import DreamRunner

    runner = DreamRunner(_ctx(request))

    def _log_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            import logging

            logging.getLogger(__name__).error("dream run failed", exc_info=task.exception())

    task = asyncio.create_task(runner.maybe_run(trigger="manual"))
    task.add_done_callback(_log_failure)
    return {"ok": True, "started": True}


@router.get("/api/dreams/announcements")
async def list_dream_announcements(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from bob_server.services.dream import DreamStore

    rows = await DreamStore(_ctx(request)).announce_history()
    return {"announcements": rows}


async def db_get_resolution(db: Any, resolution_id: str) -> Any:
    return await db.fetch_one("SELECT * FROM dream_resolutions WHERE id = ?", (resolution_id,))
