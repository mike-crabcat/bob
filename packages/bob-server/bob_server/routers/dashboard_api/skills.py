"""Dashboard API: Skills and skill delegations."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/skills/installed")
async def get_installed_skills(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir.expanduser().resolve()
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return {"skills": []}

    from bob_server.services.skill_loader import _parse_frontmatter

    skills: list[dict[str, Any]] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        md = child / "skill.md"
        if not md.is_file():
            md = child / "SKILL.md"
        if not md.is_file():
            continue
        content = md.read_text(encoding="utf-8").strip()
        fm = _parse_frontmatter(content)
        skills.append({
            "name": child.name,
            "description": fm.get("description", ""),
            "trigger": fm.get("trigger", ""),
            "has_helper": (child / "helper.py").is_file(),
            "has_pyproject": (child / "pyproject.toml").is_file(),
        })
    return {"skills": skills}


@router.get("/api/skills/delegations")
async def get_skill_delegations(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_delegations'"
    )
    if not table_exists:
        return {"delegations": []}

    rows = await db.fetch_all(
        """SELECT id, session_key, user_story, plan, status,
                  files_created_json, result_summary, cost_usd,
                  error_message, created_at, updated_at
           FROM skill_delegations
           ORDER BY created_at DESC LIMIT 50"""
    )
    delegations: list[dict[str, Any]] = []
    for row in rows:
        files: list[str] = []
        if row["files_created_json"]:
            try:
                files = json.loads(row["files_created_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        delegations.append({
            "id": row["id"],
            "session_key": row["session_key"],
            "user_story": row["user_story"],
            "plan_preview": (row["plan"] or "")[:300],
            "status": row["status"],
            "files_created": files,
            "result_summary": row["result_summary"],
            "cost_usd": row["cost_usd"] or 0,
            "error_message": row["error_message"],
            "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]),
        })
    return {"delegations": delegations}


@router.get("/api/skills/delegations/{delegation_id}")
async def get_skill_delegation_detail(request: Request, delegation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one(
        """SELECT id, session_key, user_story, plan, status,
                  files_created_json, result_summary, cost_usd,
                  error_message, created_at, updated_at
           FROM skill_delegations WHERE id = ?""",
        (delegation_id,),
    )
    if not row:
        return {"error": "not found"}
    files: list[str] = []
    if row["files_created_json"]:
        try:
            files = json.loads(row["files_created_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": row["id"],
        "session_key": row["session_key"],
        "user_story": row["user_story"],
        "plan": row["plan"],
        "status": row["status"],
        "files_created": files,
        "result_summary": row["result_summary"],
        "cost_usd": row["cost_usd"] or 0,
        "error_message": row["error_message"],
        "created_at": _utc(row["created_at"]),
        "updated_at": _utc(row["updated_at"]),
    }


@router.post("/api/skills/delegations/{delegation_id}/implement")
async def implement_skill_delegation(request: Request, delegation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from bob_server.context import AppContext
    from bob_server.services.skill_developer_service import SkillDeveloperService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SkillDeveloperService(ctx)
    try:
        result = await svc.implement_skill(delegation_id)
        return result
    except Exception as exc:
        logger.error("Skill implement failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/api/skills/delegations/{delegation_id}/reject")
async def reject_skill_delegation(request: Request, delegation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    reason = (body.get("reason") or "").strip()

    from bob_server.context import AppContext
    from bob_server.services.skill_developer_service import SkillDeveloperService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SkillDeveloperService(ctx)
    try:
        result = await svc.reject_skill(delegation_id, reason)
        return result
    except Exception as exc:
        logger.error("Skill reject failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Subagents ─────────────────────────────────────────────────────────────────


