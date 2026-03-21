"""HTTP routes for persisted notifications."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from cyborg.dependencies import get_notification_service
from cyborg.models import (
    NotificationAcknowledgeRequest,
    NotificationEntityType,
    NotificationResponse,
    NotificationStatus,
)
from cyborg.services.notification_service import NotificationService


router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    status: NotificationStatus | None = NotificationStatus.PENDING,
    entity_type: NotificationEntityType | None = None,
    limit: int = 100,
    service: NotificationService = Depends(get_notification_service),
) -> list[NotificationResponse]:
    return await service.list_notifications(status=status, entity_type=entity_type, limit=limit)


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(
    notification_id: UUID,
    service: NotificationService = Depends(get_notification_service),
) -> NotificationResponse:
    return await service.get_notification(str(notification_id))


@router.post("/{notification_id}/acknowledge", response_model=NotificationResponse)
async def acknowledge_notification(
    notification_id: UUID,
    payload: NotificationAcknowledgeRequest | None = None,
    service: NotificationService = Depends(get_notification_service),
) -> NotificationResponse:
    return await service.acknowledge_notification(
        str(notification_id),
        payload or NotificationAcknowledgeRequest(),
    )


@router.post("/process-due", response_model=dict[str, int])
async def process_due_notifications(
    service: NotificationService = Depends(get_notification_service),
) -> dict[str, int]:
    processed = await service.process_due_notifications()
    return {"processed": processed}
