"""API endpoints for dispatch tracking."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from cyborg_server.models import DispatchResponse
from cyborg_server.services.dispatch_service import DispatchService
from cyborg_server.dependencies import get_dispatch_service

router = APIRouter(prefix="/api/v1/dispatches", tags=["dispatches"])


@router.get("/active", response_model=list[DispatchResponse])
async def list_active_dispatches(
    limit: int = 100,
    service: DispatchService = Depends(get_dispatch_service),
) -> list[DispatchResponse]:
    return await service.list_active_dispatches(limit=limit)


@router.get("/stuck", response_model=list[DispatchResponse])
async def list_stuck_dispatches(
    timeout_minutes: float = 60.0,
    service: DispatchService = Depends(get_dispatch_service),
) -> list[DispatchResponse]:
    return await service.get_stuck_dispatches(timeout_minutes=timeout_minutes)


@router.post("/{dispatch_id}/tap", response_model=DispatchResponse)
async def tap_dispatch(
    dispatch_id: str,
    service: DispatchService = Depends(get_dispatch_service),
) -> DispatchResponse:
    result = await service.tap_dispatch(dispatch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Dispatch not found or not active")
    return result


@router.post("/{dispatch_id}/cancel", response_model=DispatchResponse)
async def cancel_dispatch(
    dispatch_id: str,
    service: DispatchService = Depends(get_dispatch_service),
) -> DispatchResponse:
    result = await service.cancel_dispatch(dispatch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Dispatch not found or not active")
    return result
