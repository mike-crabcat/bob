"""Built-in tool merge honors an explicit empty tool list (2026-10-07).

Zero-tool eval drafting calls leaked the OpenAI ``web_search`` built-in —
there was no way to say "no tools this call". ``_merge_tools`` now treats
``tools=[]`` as an opt-out of built-ins while ``None`` (the default) and
non-empty lists keep them.
"""

from __future__ import annotations

from types import SimpleNamespace

from server.config import OpenAISettings
from server.services.openai_service import OpenAIService


def _service(*, web_search: bool = True) -> OpenAIService:
    svc = object.__new__(OpenAIService)
    svc._get_settings = lambda: SimpleNamespace(
        openai=OpenAISettings(api_key="k", web_search_enabled=web_search))
    return svc


def test_none_tools_keeps_builtin():
    merged = _service()._merge_tools(model="gpt-6-luna")
    assert [t["type"] for t in merged] == ["web_search"]


def test_nonempty_tools_still_get_builtin_prepended():
    tool = {"type": "function", "name": "send_whatsapp_message"}
    merged = _service()._merge_tools([tool], model="gpt-6-luna")
    assert [t["type"] for t in merged] == ["web_search", "function"]


def test_explicit_empty_list_opts_out_of_builtins():
    """tools=[] is the only way a caller says "no tools, web_search
    included" — the drafting-case leak."""
    assert _service()._merge_tools([], model="gpt-6-luna") == []


def test_flag_off_never_merges_builtin():
    tool = {"type": "function", "name": "send_whatsapp_message"}
    assert _service(web_search=False)._merge_tools([tool], model="gpt-6-luna") \
        == [tool]
