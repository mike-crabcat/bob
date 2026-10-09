"""Hosted web_search counts as a tool call (2026-10-09 AI Doom: a luna
answer that searched the web was stamped 'UNVERIFIED: this turn ran no
tools')."""

from __future__ import annotations

import json
from types import SimpleNamespace

from server.services.backburner import _transcript_tool_names
from server.services.openai_service import OpenAIService
from tests.test_self_wrap import install_client  # noqa: F401  (fixture)


def _search_then_answer():
    return SimpleNamespace(
        output=[
            SimpleNamespace(type="web_search_call", status="completed",
                            action=SimpleNamespace(query="cheapest resin printer aliexpress")),
            SimpleNamespace(type="message", role="assistant",
                            content=[SimpleNamespace(type="output_text",
                                                     text="Creality HALOT R6, US$110")]),
        ],
        output_text="Creality HALOT R6, US$110",
        usage=SimpleNamespace(total_tokens=10, input_tokens=5, output_tokens=5),
        status="completed", refusal=None,
    )


async def test_hosted_web_search_reported_to_on_tool_call(ctx, install_client):  # noqa: F811
    install_client(script=[_search_then_answer()])
    seen = []

    async def on_tool_call(name, args, summary):
        seen.append((name, args))

    out = await OpenAIService(ctx).run_turn(
        [{"role": "user", "content": "cheapest resin printer?"}], [], {},
        model="gpt-6-luna", dispatch_id="d-ws", session_key="s-ws",
        on_tool_call=on_tool_call)
    assert "HALOT" in out
    assert seen == [("web_search", {"query": "cheapest resin printer aliexpress"})]


def test_transcript_scan_counts_hosted_search():
    items = [{"role": "user", "content": "q"},
             {"type": "web_search_call", "status": "completed"},
             {"type": "message", "role": "assistant", "content": "a"}]
    assert _transcript_tool_names(json.dumps(items)) == ["web_search"]
