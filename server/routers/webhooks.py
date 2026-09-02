"""API router for webhook management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from bob_server.dependencies import get_app_context
from bob_server.context import AppContext
from bob_server.services.webhook_service import WebhookService


router = APIRouter(tags=["webhooks"])


def get_webhook_service(ctx: AppContext = Depends(get_app_context)) -> WebhookService:
    """Dependency to get webhook service."""
    return WebhookService(ctx)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    payload: dict[str, Any],
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Create a new webhook configuration."""
    config = await service.create_config(
        name=payload["name"],
        url=payload["url"],
        secret=payload["secret"],
        events=payload["events"],
        retry_count=payload.get("retry_count", 3),
    )
    return {
        "id": config.id,
        "name": config.name,
        "url": config.url,
        "events": config.events,
        "retry_count": config.retry_count,
        "is_active": config.is_active,
    }


@router.get("")
async def list_webhooks(
    active_only: bool = True,
    service: WebhookService = Depends(get_webhook_service),
) -> list[dict[str, Any]]:
    """List all webhook configurations."""
    configs = await service.list_configs(active_only=active_only)
    return [
        {
            "id": c.id,
            "name": c.name,
            "url": c.url,
            "events": c.events,
            "retry_count": c.retry_count,
            "is_active": c.is_active,
        }
        for c in configs
    ]


@router.get("/by-name/{name}")
async def get_webhook_by_name(
    name: str,
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Get a webhook configuration by name."""
    config = await service.get_config_by_name(name)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {
        "id": config.id,
        "name": config.name,
        "url": config.url,
        "events": config.events,
        "retry_count": config.retry_count,
        "is_active": config.is_active,
    }


@router.put("/{config_id}")
async def update_webhook(
    config_id: str,
    payload: dict[str, Any],
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Update a webhook configuration."""
    config = await service.update_config(
        config_id,
        url=payload.get("url"),
        secret=payload.get("secret"),
        events=payload.get("events"),
        retry_count=payload.get("retry_count"),
        is_active=payload.get("is_active"),
    )
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {
        "id": config.id,
        "name": config.name,
        "url": config.url,
        "events": config.events,
        "retry_count": config.retry_count,
        "is_active": config.is_active,
    }


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    config_id: str,
    service: WebhookService = Depends(get_webhook_service),
) -> None:
    """Delete a webhook configuration."""
    success = await service.delete_config(config_id)
    if not success:
        raise HTTPException(status_code=404, detail="Webhook not found")


@router.get("/deliveries")
async def list_deliveries(
    webhook_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    service: WebhookService = Depends(get_webhook_service),
) -> list[dict[str, Any]]:
    """List webhook deliveries."""
    deliveries = await service.list_deliveries(
        webhook_id=webhook_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return deliveries


@router.get("/deliveries/{delivery_id}")
async def get_delivery(
    delivery_id: str,
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Get a delivery by ID."""
    delivery = await service.get_delivery(delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(
    delivery_id: str,
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Retry a failed delivery."""
    delivery = await service.get_delivery(delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    
    # Reset status to pending and attempt delivery
    from bob_server.services.webhook_service import WebhookStatus
    if delivery["status"] == WebhookStatus.DELIVERED:
        raise HTTPException(status_code=400, detail="Delivery already succeeded")
    
    # Attempt delivery
    success = await service._attempt_delivery(delivery_id)
    
    # Get updated delivery
    updated = await service.get_delivery(delivery_id)
    return {
        "success": success,
        "delivery": updated,
    }


@router.post("/process-pending")
async def process_pending_deliveries(
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Process all pending deliveries that are due for retry."""
    count = await service.process_pending_deliveries()
    return {"processed": count}


@router.get("/{config_id}")
async def get_webhook(
    config_id: str,
    service: WebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Get a webhook configuration by ID."""
    config = await service.get_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {
        "id": config.id,
        "name": config.name,
        "url": config.url,
        "events": config.events,
        "retry_count": config.retry_count,
        "is_active": config.is_active,
    }
