"""models.yaml effort defaults reach the Responses API request: main-turn
paths (chat, chat_stream, chat_with_tools, chat_stream_with_tools) pass no
explicit effort, so the per-model default must be merged in — and an explicit
caller hint must win over it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.services import model_registry
from server.services.openai_service import OpenAIService


YAML = """\
effort:
  z-ai/glm-5.3-flash: medium
"""


def _service(tmp_path, captured):
    """OpenAIService pointed at a recording fake client — no network."""
    (tmp_path / "models.yaml").write_text(YAML, encoding="utf-8")
    model_registry._models_cache = None
    svc = object.__new__(OpenAIService)
    settings = SimpleNamespace(
        config_dir=tmp_path,
        openai=SimpleNamespace(
            api_key="test-key", base_url="http://localhost:1",
            default_model="gpt-test", memory_model="",
            web_search_enabled=False),
        openrouter=SimpleNamespace(enabled=True),
        self_wrap=SimpleNamespace(
            enabled=True, duration_fraction=0.75, iteration_margin=3),
    )
    svc._get_settings = lambda: settings

    class _FakeResponses:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if kwargs.get("stream"):

                async def _events():  # empty event stream
                    return
                    yield  # pragma: no cover
                return _events()
            return SimpleNamespace(output=[], output_text="", usage=None)

    svc._client_for = lambda model: SimpleNamespace(responses=_FakeResponses())
    return svc


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    model_registry._models_cache = None
    yield
    model_registry._models_cache = None


@pytest.mark.asyncio
async def test_yaml_default_reaches_chat(tmp_path):
    captured: list[dict] = []
    svc = _service(tmp_path, captured)
    await svc.chat([{"role": "user", "content": "hi"}], model="z-ai/glm-5.3-flash")
    assert captured[0].get("reasoning") == {"effort": "medium"}


@pytest.mark.asyncio
async def test_explicit_effort_wins(tmp_path):
    captured: list[dict] = []
    svc = _service(tmp_path, captured)
    await svc.chat([{"role": "user", "content": "hi"}],
                   model="z-ai/glm-5.3-flash", reasoning_effort="low")
    assert captured[0].get("reasoning") == {"effort": "low"}


@pytest.mark.asyncio
async def test_unconfigured_model_gets_no_reasoning_kwarg(tmp_path):
    captured: list[dict] = []
    svc = _service(tmp_path, captured)
    await svc.chat([{"role": "user", "content": "hi"}], model="z-ai/other-model")
    assert "reasoning" not in captured[0]


@pytest.mark.asyncio
async def test_yaml_default_reaches_chat_with_tools(tmp_path):
    captured: list[dict] = []
    svc = _service(tmp_path, captured)
    await svc.chat_with_tools(
        [{"role": "user", "content": "hi"}], tools=[], tool_handlers={},
        model="z-ai/glm-5.3-flash")
    assert captured and all(c.get("reasoning") == {"effort": "medium"} for c in captured)


@pytest.mark.asyncio
async def test_yaml_default_reaches_chat_stream(tmp_path):
    captured: list[dict] = []
    svc = _service(tmp_path, captured)
    async for _ in svc.chat_stream([{"role": "user", "content": "hi"}],
                                   model="z-ai/glm-5.3-flash"):
        pass
    assert captured[0].get("reasoning") == {"effort": "medium"}
