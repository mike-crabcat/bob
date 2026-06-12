"""HTTP routes for session route registry management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from bob_server.dependencies import get_session_route_service
from bob_server.models import SessionRouteCreate, SessionRouteResponse, SessionRouteUpdate
from bob_server.services.session_route_service import SessionRouteService


router = APIRouter(prefix="/api/v1/session-routes", tags=["session-routes"])


@router.post("", response_model=SessionRouteResponse, status_code=status.HTTP_201_CREATED)
async def create_session_route(
    payload: SessionRouteCreate,
    service: SessionRouteService = Depends(get_session_route_service),
) -> SessionRouteResponse:
    return await service.create_route(payload)


@router.get("", response_model=list[SessionRouteResponse])
async def list_session_routes(
    channel: str | None = None,
    active_only: bool = True,
    service: SessionRouteService = Depends(get_session_route_service),
) -> list[SessionRouteResponse]:
    return await service.list_routes(channel=channel, active_only=active_only)


@router.get("/{route_id}", response_model=SessionRouteResponse)
async def get_session_route(
    route_id: UUID,
    service: SessionRouteService = Depends(get_session_route_service),
) -> SessionRouteResponse:
    return await service.get_route(str(route_id))


@router.put("/{route_id}", response_model=SessionRouteResponse)
async def update_session_route(
    route_id: UUID,
    payload: SessionRouteUpdate,
    service: SessionRouteService = Depends(get_session_route_service),
) -> SessionRouteResponse:
    return await service.update_route(str(route_id), payload)


@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session_route(
    route_id: UUID,
    service: SessionRouteService = Depends(get_session_route_service),
) -> Response:
    await service.delete_route(str(route_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
