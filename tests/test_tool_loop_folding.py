"""Tool-loop folding tests (2026-09-10 diagnosis → docs plan in the module
docstring of server/services/tool_loop_folding.py).

Covers the folder's contract (aged/oversized only, media and small and
recent exempt, idempotent), the history view (non-destructive, gated by
size), and the loop integration via a fake Responses client: the wire copy
shrinks while the canonical message list keeps full history; the kill
switch restores old behavior exactly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.config import Settings, ToolLoopSettings
from server.services import tool_loop_folding as tlf
from server.services.openai_service import OpenAIService

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "server" / "schemas"


def _result_item(call_id: str, text: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


# ─── folder unit contract ─────────────────────────────────────────────

def _msg_set():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "a", "name": "bash",
         "arguments": "{}"},
        _result_item("a", "SMALL-RESULT"),                     # under threshold
        {"type": "function_call", "call_id": "b", "name": "bash",
         "arguments": "{}"},
        _result_item("b", "HEAD" + "x" * 9000 + "TAILMARK"),   # big, aged
        {"type": "function_call", "call_id": "c", "name": "bash",
         "arguments": "{}"},
        _result_item("c", "y" * 9000),                         # big, recent
        {"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64," + "z" * 9000},
        ]},                                                     # media block
    ]


def test_fold_aged_only_big_only_text_only():
    msgs = _msg_set()
    dropped = tlf.fold_aged_tool_outputs(msgs, keep_last=1)
    assert dropped > 0
    by_call = {m.get("call_id"): m for m in msgs
               if m.get("type") == "function_call_output"}
    assert tlf.ELISION_TAG in by_call["b"]["output"]          # aged big folded
    assert "HEAD" in by_call["b"]["output"]                   # head kept
    assert "TAILMARK" in by_call["b"]["output"]               # tail kept
    assert by_call["c"]["output"] == "y" * 9000               # recent verbatim
    assert by_call["a"]["output"] == "SMALL-RESULT"           # small verbatim
    media = msgs[-1]
    assert media["content"][0]["image_url"].endswith("z" * 9000)  # untouched
    # call itself always survives
    assert any(m.get("call_id") == "b" and m.get("name") == "bash"
               for m in msgs)


def test_fold_is_idempotent():
    msgs = _msg_set()
    first = tlf.fold_aged_tool_outputs(msgs, keep_last=1)
    assert tlf.fold_aged_tool_outputs(msgs, keep_last=1) == 0
    assert first > 0


def test_fold_all_recent_kept_when_few_results():
    msgs = [_result_item("only", "x" * 9000)]
    assert tlf.fold_aged_tool_outputs(msgs, keep_last=4) == 0
    assert msgs[0]["output"] == "x" * 9000


# ─── history view ─────────────────────────────────────────────────────

def test_view_returns_same_list_when_history_small():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "u"}]
    assert tlf.iteration_view(msgs, 2, history_keep=20) is msgs


def test_view_trims_history_but_not_canonical():
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"m{i} " + "x" * 800}
             for i in range(60)]
    base_len = len(msgs)
    msgs.append(_result_item("t", "tool"))
    view = tlf.iteration_view(msgs, base_len, history_keep=20)
    assert view[0]["role"] == "system"
    assert len(view) == 1 + 20 + 1
    assert "m0 " not in str(view)          # old history off the wire
    assert view[-1]["call_id"] == "t"      # transcript always present
    assert len(msgs) == base_len + 1       # canonical untouched


def test_view_without_leading_system_keeps_all_headless():
    msgs = [{"role": "user", "content": f"m{i} " + "x" * 800}
            for i in range(60)]
    base_len = len(msgs)
    view = tlf.iteration_view(msgs, base_len, history_keep=10)
    assert len(view) == 10 + 1 or len(view) == 10  # transcript slice may be empty
    assert all(m.get("role") == "user" for m in view)


# ─── loop integration (fake Responses client) ─────────────────────────

class _FakeResponses:
    """Scripted Responses API: one round per script entry. Records every
    input it was sent. Items must be attribute-objects (the loop reads
    getattr(item, "type") — API item objects, not dicts)."""

    def __init__(self, script: list[list[Any]]):
        self.script = script
        self.sent: list[list[dict[str, Any]]] = []

    async def create(self, *, input: list[dict[str, Any]], **kw) -> Any:
        self.sent.append(input)
        items = self.script.pop(0)
        usage = SimpleNamespace(
            input_tokens=100, output_tokens=10, total_tokens=110,
            input_tokens_details=SimpleNamespace(cached_tokens=50))
        out_text = next((i.content[0].text for i in items
                         if getattr(i, "type", None) == "message"), "")
        return SimpleNamespace(output=items, usage=usage,
                               output_text=out_text, status="completed",
                               refusal=None)


def _fc(call_id: str, name: str = "echo") -> Any:
    return SimpleNamespace(type="function_call", call_id=call_id,
                           name=name, arguments="{}")


def _msg(text: str) -> Any:
    return SimpleNamespace(type="message", role="assistant",
                           content=[SimpleNamespace(type="output_text",
                                                    text=text)])


@pytest.fixture
def ctx(tmp_path):
    from server.context import AppContext
    return AppContext(db=None, settings=Settings(
        data_dir=tmp_path / "d", config_dir=tmp_path / "c",
        db_path=tmp_path / "d" / "bob.db"))


async def _run_loop(ctx, messages, script, tool_results):
    fake = _FakeResponses(script)
    svc = OpenAIService(ctx)
    svc._client_for = lambda model: SimpleNamespace(responses=fake)  # type: ignore
    handlers = {name: (lambda r=r: _mk_result(r)) for name, r in tool_results.items()}

    async def _mk_result(r):
        return r

    result = await svc.chat_with_tools(
        messages, tools=[], tool_handlers=handlers,
        model="test-model", max_iterations=10)
    return fake, result


async def test_loop_folds_and_views_big_turns(ctx):
    ctx.settings.tool_loop = ToolLoopSettings(
        folding_enabled=True, fold_keep_last=1, fold_size_threshold=4000,
        history_view_keep=10, history_view_trigger_chars=20000)
    big = "A" * 8000
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": f"m{i} " + "x" * 600}
                 for i in range(60)]  # ~36k chars history → view on
    script = [
        [_fc("c1")],
        [_fc("c2")],
        [_msg("done: first result was 8000 chars")],
    ]
    fake, result = await _run_loop(
        ctx, messages, script, {"echo": big})
    assert "8000" in result
    # history on the wire is trimmed on every request…
    for sent in fake.sent:
        assert len(sent) < len(messages)
        assert sent[0]["role"] == "system"
    # …while the canonical list keeps the full history
    assert sum(1 for m in messages if m.get("role") == "user") == 60
    # iteration 3's request: c1 folded (aged), c2 verbatim (recent)
    third = fake.sent[2]
    outputs = {m.get("call_id"): m for m in third
               if m.get("type") == "function_call_output"}
    assert tlf.ELISION_TAG in outputs["c1"]["output"]
    assert outputs["c2"]["output"] == big
    assert len(outputs["c1"]["output"]) < 3000


async def test_loop_kill_switch_restores_exact_behavior(ctx):
    ctx.settings.tool_loop = ToolLoopSettings(folding_enabled=False)
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": f"m{i} " + "x" * 600}
                 for i in range(60)]
    script = [[_fc("c1")], [_msg("ok")]]
    fake, _ = await _run_loop(ctx, messages, script, {"echo": "B" * 9000})
    # every request is the full canonical list, nothing folded
    for sent in fake.sent:
        assert len(sent) == len(messages)
    outputs = [m for m in fake.sent[-1]
               if m.get("type") == "function_call_output"]
    assert outputs and outputs[0]["output"] == "B" * 9000


async def test_loop_small_turn_untouched(ctx):
    # small history + small result: wire == canonical, no markers
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "run it"}]
    script = [[_fc("c1")], [_msg("ok")]]
    fake, _ = await _run_loop(ctx, messages, script, {"echo": "small"})
    for sent in fake.sent:
        assert sent is messages or sent == messages
    assert not any(tlf.ELISION_TAG in str(m) for m in messages)
