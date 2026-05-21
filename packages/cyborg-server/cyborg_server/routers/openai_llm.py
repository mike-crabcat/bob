"""OpenAI evaluation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from cyborg_server.dependencies import get_app_context
from cyborg_server.context import AppContext
from cyborg_server.services.openai_service import OpenAIService

router = APIRouter(prefix="/api/v1/openai", tags=["openai"])


class PromptRequest(BaseModel):
    prompt: str
    model: str | None = None
    temperature: float = 0.7
    stream: bool = False


class PromptResponse(BaseModel):
    content: str
    model: str


@router.post("/prompt", response_model=PromptResponse)
async def prompt_openai(
    request: PromptRequest,
    ctx: AppContext = Depends(get_app_context),
) -> PromptResponse:
    """Send a prompt to OpenAI and return the response."""
    service = OpenAIService(ctx)
    messages = [{"role": "user", "content": request.prompt}]

    if request.stream:
        chunks: list[str] = []
        async for chunk in service.chat_stream(
            messages=messages,
            model=request.model,
            temperature=request.temperature,
        ):
            chunks.append(chunk)
        result = "".join(chunks)
    else:
        result = await service.chat(
            messages=messages,
            model=request.model,
            temperature=request.temperature,
        )

    return PromptResponse(
        content=result,
        model=request.model or "gpt-4.1-mini",
    )
