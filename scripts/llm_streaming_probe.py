"""LLM streaming/reasoning capability probe (Phase 0 of the trace uplift).

Standalone network script — NOT part of the pytest suite. Answers, per rail
(OpenAI-direct and OpenRouter), the questions the trace uplift's defaults
depend on:

  (a) non-streaming reasoning={"effort", "summary": "auto"} — do reasoning
      output items carry non-empty summaries? Does the param 400?
  (b) streaming event inventory with tools — which response.* events arrive?
  (c) does response.completed carry full output + usage (so the tool loop can
      consume streamed rounds exactly like buffered ones)? response.incomplete
      shape under a small max_output_tokens cap?
  (d) tool_choice="required" + stream=True (evals' force_first_tool_choice)?
  (e) does the OpenRouter/GLM rail surface reasoning in Responses shape at all?

Usage: uv run python scripts/llm_streaming_probe.py [model ...]
Defaults to both production rails (gpt-5.6-sol, z-ai/glm-5.3-flash).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import Settings  # noqa: E402
from server.services import model_registry  # noqa: E402
from server.services.openai_service import _get_cached_client  # noqa: E402

# Tiny no-op weather tool — enough shape for tool_choice/function_call probing.
TOOLS = [{
    "type": "function",
    "name": "get_weather",
    "description": "Get current weather for a city",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}]

PROMPT = [{"role": "user", "content": "What's the weather in Perth right now? Use the tool."}]

INTERESTING_PREFIX = "response."


def _cli(model: str) -> Any:
    """Client for this model, resolved exactly as OpenAIService._client_for does."""
    settings = Settings.from_env()
    if model_registry.provider_for(model) == model_registry.PROVIDER_OPENROUTER:
        return _get_cached_client(
            settings.openrouter.api_key, settings.openrouter.base_url,
            default_headers={"X-Title": "bob"})
    return _get_cached_client(settings.openai.api_key, settings.openai.base_url)


def _effort(model: str) -> str:
    settings = Settings.from_env()
    default = model_registry.effort_defaults(settings.config_dir).get(model)
    return default or "low"


def _out_types(response: object) -> list[str]:
    return [getattr(item, "type", "?") for item in (getattr(response, "output", None) or [])]


def _summary_text(response: object) -> str:
    """Concatenate reasoning text from a completed response object.

    Two shapes (probe finding): OpenAI-direct fills ``item.summary`` (when
    summary requested + model thinks hard enough); OpenRouter/GLM fills
    ``item.content`` with reasoning_text parts from the raw reasoning stream.
    """
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "reasoning":
            continue
        for s in (getattr(item, "summary", None) or []):
            text = getattr(s, "text", None) or ""
            if text:
                parts.append(text)
        for c in (getattr(item, "content", None) or []):
            if getattr(c, "type", None) == "reasoning_text":
                text = getattr(c, "text", None) or ""
                if text:
                    parts.append(text)
    return " ".join(parts)


async def probe_a(model: str) -> dict:
    """Non-streaming summary request."""
    try:
        r = await _cli(model).responses.create(
            model=model,
            input=PROMPT,
            tools=TOOLS,
            reasoning={"effort": _effort(model), "summary": "auto"},
            max_output_tokens=4000,
        )
        return {
            "ok": True,
            "output_types": _out_types(r),
            "summary_chars": len(_summary_text(r)),
            "summary_preview": _summary_text(r)[:200],
            "usage": {"in": getattr(r.usage, "input_tokens", None),
                      "out": getattr(r.usage, "output_tokens", None)},
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


async def probe_stream(model: str, *, label: str, extra: dict | None = None,
                       max_output_tokens: int = 4000, prompt: list | None = None,
                       effort: str | None = None,
                       want_tools: bool = True) -> dict:
    """Streaming event inventory + completed-response completeness."""
    events: list[str] = []
    completed = None
    incomplete = None
    failed = None
    sample: dict[str, str] = {}
    kwargs: dict[str, Any] = {}
    if want_tools:
        kwargs["tools"] = TOOLS  # NB: OpenRouter 400s on tools=null (probe finding)
    try:
        stream = await _cli(model).responses.create(
            model=model,
            input=prompt or PROMPT,
            stream=True,
            reasoning={"effort": effort or _effort(model), "summary": "auto"},
            max_output_tokens=max_output_tokens,
            **kwargs,
            **(extra or {}),
        )
        async for ev in stream:
            et = getattr(ev, "type", "?")
            if et not in events:
                events.append(et)
                # keep one sample payload per event type for the report
                try:
                    d = ev.model_dump()
                    sample[et] = json.dumps(d, default=str)[:400]
                except Exception:
                    sample[et] = "?"
            if et == "response.completed":
                completed = ev.response
            elif et == "response.incomplete":
                incomplete = ev.response
            elif et in ("response.failed", "error"):
                failed = et
    except Exception as e:
        return {"label": label, "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}

    resp = completed or incomplete
    return {
        "label": label,
        "ok": True,
        "events": [e for e in events if e.startswith(INTERESTING_PREFIX)],
        "failed_event": failed,
        "completed_present": completed is not None,
        "incomplete_present": incomplete is not None,
        "final_output_types": _out_types(resp) if resp is not None else None,
        "final_usage": ({"in": getattr(resp.usage, "input_tokens", None),
                         "out": getattr(resp.usage, "output_tokens", None)}
                        if resp is not None and getattr(resp, "usage", None) else None),
        "summary_chars": len(_summary_text(resp)) if resp is not None else None,
        "samples": sample,
    }


async def probe_incomplete(model: str) -> dict:
    """Small cap → response.incomplete shape (does output still carry items?)."""
    return await probe_stream(model, label="incomplete",
                              max_output_tokens=120)  # below reasoning burn → truncation


async def probe_text(model: str, effort: str | None = None) -> dict:
    """(f) text round at higher effort — do reasoning summary events/text appear?"""
    return await probe_stream(
        model,
        label=f"text@{effort or _effort(model)} (no tools)",
        prompt=[{"role": "user",
                 "content": "A bat and ball cost $1.10 total; the bat costs $1.00 more than "
                            "the ball. What does the ball cost? Reason carefully, then answer."}],
        want_tools=False,
        effort=effort,
        max_output_tokens=6000,
    )


async def probe_required(model: str) -> dict:
    """tool_choice=required + stream (evals' force_first_tool_choice path)."""
    return await probe_stream(model, label="tool_choice_required",
                              extra={"tool_choice": "required"})


async def run(model: str) -> None:
    print(f"\n{'=' * 70}\nMODEL {model}  (provider={model_registry.provider_for(model)}, "
          f"effort={_effort(model)})\n{'=' * 70}")
    for name, coro in [
        ("(a) non-streaming summary", probe_a(model)),
        ("(b) streaming inventory", probe_stream(model, label="streaming")),
        ("(c) small-cap incomplete", probe_incomplete(model)),
        ("(d) tool_choice=required + stream", probe_required(model)),
        ("(f) text round, effort=high", probe_text(model, effort="high")),
    ]:
        result = await coro
        print(f"\n--- {name} ---")
        print(json.dumps(result, indent=2, default=str)[:2400])


async def main() -> None:
    models = sys.argv[1:] or ["gpt-5.6-sol", "z-ai/glm-5.3-flash"]
    for m in models:
        await run(m)


if __name__ == "__main__":
    asyncio.run(main())
