"""HTTP routes for task management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, model_validator

from cyborg.dependencies import get_task_service

from cyborg.models import (
    TaskBlockRequest,
    TaskFailureRequest,
    TaskFileCreate,
    TaskFileListResponse,
    TaskFileResponse,
    TaskHistoryResponse,
    TaskResponse,
    TaskRetryRequest,
    TaskStatus,
    TaskStepCreate,
    TaskStepResponse,
    TaskUnblockRequest,
    TaskUpdate,
)
from cyborg.services.task_service import TaskService


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    status: TaskStatus | None = None,
    parent_id: UUID | None = None,
    service: TaskService = Depends(get_task_service),
) -> list[TaskResponse]:
    return await service.list_tasks(status=status, parent_id=str(parent_id) if parent_id else None)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, service: TaskService = Depends(get_task_service)) -> TaskResponse:
    return await service.get_task(str(task_id))


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.update_task(str(task_id), payload)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: UUID, service: TaskService = Depends(get_task_service)) -> Response:
    await service.delete_task(str(task_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/start", response_model=TaskResponse)
async def start_task(task_id: UUID, service: TaskService = Depends(get_task_service)) -> TaskResponse:
    return await service.start_task(str(task_id))


class TaskCompleteRequest(BaseModel):
    result_summary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_result_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "result_summary" not in value and "result" in value:
            return {**value, "result_summary": value["result"]}
        return value


@router.post("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(
    task_id: UUID,
    payload: TaskCompleteRequest | None = None,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.complete_task(str(task_id), payload.result_summary if payload else None)


class TaskSubmitRequest(BaseModel):
    result_summary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_result_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "result_summary" not in value and "result" in value:
            return {**value, "result_summary": value["result"]}
        return value


@router.post("/{task_id}/submit", response_model=TaskResponse)
async def submit_task(
    task_id: UUID,
    payload: TaskSubmitRequest | None = None,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.submit_task(str(task_id), payload.result_summary if payload else None)


@router.post("/{task_id}/fail", response_model=TaskResponse)
async def fail_task(
    task_id: UUID,
    payload: TaskFailureRequest,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.fail_task(str(task_id), payload)


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    task_id: UUID,
    payload: TaskRetryRequest,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.retry_task(str(task_id), payload)


@router.post("/{task_id}/block", response_model=TaskResponse)
async def block_task(
    task_id: UUID,
    payload: TaskBlockRequest,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.block_task(str(task_id), payload)


@router.post("/{task_id}/unblock", response_model=TaskResponse)
async def unblock_task(
    task_id: UUID,
    payload: TaskUnblockRequest | None = None,
    service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    return await service.unblock_task(str(task_id), payload or TaskUnblockRequest())


@router.get("/{task_id}/steps", response_model=list[TaskStepResponse])
async def list_steps(task_id: UUID, service: TaskService = Depends(get_task_service)) -> list[TaskStepResponse]:
    return await service.list_steps(str(task_id))


@router.post("/{task_id}/steps", response_model=TaskStepResponse, status_code=status.HTTP_201_CREATED)
async def upsert_step(
    task_id: UUID,
    payload: TaskStepCreate,
    service: TaskService = Depends(get_task_service),
) -> TaskStepResponse:
    return await service.upsert_step(str(task_id), payload)


@router.get("/{task_id}/history", response_model=list[TaskHistoryResponse])
async def list_history(task_id: UUID, service: TaskService = Depends(get_task_service)) -> list[TaskHistoryResponse]:
    return await service.list_history(str(task_id))


@router.get("/{task_id}/files", response_model=TaskFileListResponse)
async def list_task_files(
    task_id: UUID,
    service: TaskService = Depends(get_task_service),
) -> TaskFileListResponse:
    return await service.list_task_files(str(task_id))


class TaskFileRegisterRequest(BaseModel):
    project_id: UUID
    file: TaskFileCreate


@router.post("/{task_id}/files", response_model=TaskFileResponse, status_code=status.HTTP_201_CREATED)
async def register_task_file(
    task_id: UUID,
    payload: TaskFileRegisterRequest,
    service: TaskService = Depends(get_task_service),
) -> TaskFileResponse:
    return await service.register_task_file(str(task_id), str(payload.project_id), payload.file)


@router.delete("/{task_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_file(
    task_id: UUID,
    file_id: UUID,
    service: TaskService = Depends(get_task_service),
) -> Response:
    await service.delete_task_file(str(task_id), str(file_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
