"""Dashboard WebSocket — forwards live event bus telemetry to dashboard clients."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from cyborg_server.database import Database
from cyborg_server.services.event_bus import EventBus

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_auth(settings: Any, websocket: WebSocket) -> bool:
    if not settings.dashboard_secret_configured:
        return True
    secret = websocket.query_params.get("secret", "")
    return secret == settings.dashboard_secret


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
                            await db.execute(
                                """INSERT INTO session_agendas (session_key, agenda, updated_at)
                                   VALUES (?, ?, datetime('now'))
                                   ON CONFLICT(session_key) DO UPDATE SET agenda=excluded.agenda, updated_at=excluded.updated_at""",
                                (session_key, agenda),
                            )

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
