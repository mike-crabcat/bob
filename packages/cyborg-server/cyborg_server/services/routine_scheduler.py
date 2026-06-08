"""RoutineSchedulerTask — fires due routines via the heartbeat loop."""

from __future__ import annotations

import asyncio
import logging

from cyborg_server.services.routine_service import RoutineService

logger = logging.getLogger(__name__)


class RoutineSchedulerTask:
    """Checks for due routines and dispatches them as independent async tasks."""

    name = "routine_scheduler"

    async def run(self, ctx) -> None:  # type: ignore[override]
        svc = RoutineService(ctx)
        due = await svc.get_due_routines()

        for routine in due:
            asyncio.create_task(self._fire_routine(ctx, routine))

    async def _fire_routine(self, ctx, routine: dict) -> None:
        from cyborg_server.services.harness_service import HarnessService
        from cyborg_server.services.session_service import SessionService

        session_key = routine["session_key"]
        prompt = routine["prompt"]
        name = routine["name"]

        try:
            session_svc = SessionService(ctx)
            await session_svc.add_message(session_key, "user", prompt, channel="routine")

            harness = HarnessService(ctx)
            response = await harness.chat(prompt, session_key)

            await session_svc.add_message(session_key, "assistant", response, channel="routine")

            # Compute next run time
            from cyborg_server.cron import next_cron_occurrence
            next_at = next_cron_occurrence(routine["schedule"]).isoformat()

            svc = RoutineService(ctx)
            await svc.mark_run(routine["id"], next_at)

            logger.info("Routine '%s' fired for session %s", name, session_key)
        except Exception:
            logger.exception("Routine '%s' failed for session %s", name, session_key)
