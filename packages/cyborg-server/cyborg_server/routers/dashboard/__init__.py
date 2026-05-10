"""Dashboard router — mounts all sub-routers under /dashboard."""

from __future__ import annotations

from fastapi import APIRouter

from . import charts, contacts, emails, overview, projects
from . import approvals, calls, harness, sessions, tasks

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

router.include_router(overview.router)
router.include_router(projects.router)
router.include_router(tasks.router)
router.include_router(approvals.router)
router.include_router(emails.router)
router.include_router(calls.router)
router.include_router(contacts.router)
router.include_router(sessions.router)
router.include_router(harness.router)
router.include_router(charts.router)
