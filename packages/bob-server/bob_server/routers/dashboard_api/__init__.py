"""Dashboard HTTP API — serves page data as JSON for the React SPA.

Split into one module per domain under this package; ``router`` below aggregates
them so callers (``main.py``) can keep doing::

    app.include_router(dashboard_api.router, prefix="/dashboard")
"""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api import (
    home, contacts, calls, workspace, memory,
    frontend_errors, skills, subagents, phone, persona, dreams, auth, ops,
    conversations, goals as goals_api,
)


router = APIRouter()
router.include_router(auth.router)
router.include_router(home.router)
router.include_router(ops.router)
router.include_router(conversations.router)
router.include_router(goals_api.router)

router.include_router(contacts.router)
router.include_router(calls.router)
router.include_router(workspace.router)
router.include_router(memory.router)
router.include_router(frontend_errors.router)
router.include_router(skills.router)
router.include_router(subagents.router)
router.include_router(phone.router)
router.include_router(persona.router)
router.include_router(dreams.router)


__all__ = ["router"]
