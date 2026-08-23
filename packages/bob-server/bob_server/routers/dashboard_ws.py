"""Dashboard WebSocket — forwards live event bus telemetry to dashboard clients."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob_server.database import Database
from bob_server.services.event_bus import EventBus

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_auth(settings: Any, websocket: WebSocket) -> bool:
    """Same token and comparison path as the HTTP gate (see api_auth), with a
    WS-specific extraction: ?secret= query or the dashboard cookie."""
    import secrets as _secrets

    if not settings.api_auth_enabled:
        return True
    token = websocket.query_params.get("secret", "") or websocket.cookies.get(
        "bob_dashboard_secret", ""
    )
    return bool(token) and _secrets.compare_digest(token, settings.resolved_api_secret)


@router.websocket("/ws")
async def dashboard_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"

    settings = websocket.app.state.settings
    if not _check_auth(settings, websocket):
        await websocket.close(code=4001, reason="Unauthorized")
        logger.warning("Dashboard WS auth failed from %s", client)
        return

    db: Database = websocket.app.state.db
    event_bus: EventBus = websocket.app.state.event_bus

    logger.info("Dashboard WS connected from %s", client)

    queue = event_bus.subscribe()
    try:
        while True:
            receive_task = asyncio.create_task(websocket.receive_text())
            event_task = asyncio.create_task(queue.get())

            done, pending = await asyncio.wait(
                {receive_task, event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            for task in done:
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    continue
                except WebSocketDisconnect:
                    return
                except Exception:
                    logger.warning("Dashboard WS error", exc_info=True)
                    return

                if task is receive_task:
                    try:
                        msg = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    msg_type = msg.get("type", "")

                    if msg_type == "update.agenda":
                        session_key = msg.get("session_key", "")
                        agenda = msg.get("agenda", "")
                        if session_key:
                            from bob_server.repositories.participants import AgendaRepository
                            await AgendaRepository(db).set(
                                session_key, agenda,
                                datetime.now(timezone.utc).isoformat())

                elif task is event_task:
                    try:
                        await websocket.send_text(json.dumps(result))
                    except Exception:
                        return

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("Dashboard WS error", exc_info=True)
    finally:
        event_bus.unsubscribe(queue)
        logger.info("Dashboard WS disconnected from %s", client)
