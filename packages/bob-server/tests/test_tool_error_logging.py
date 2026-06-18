"""Tests for tool-call error logging in OpenAIService.

When a tool handler raises during dispatch, or when the LLM hallucinates a
tool name, the error should be logged at ERROR level with full context
(traceback, args, dispatch_id, session_key, log_id, call_id) so it can be
triaged from journalctl.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bob_server.services.openai_service import OpenAIService


def _make_fake_client(*, tool_name: str, call_id: str, arguments: str = "{}") -> tuple[object, list[dict]]:
    """Build a fake OpenAI client whose responses.create returns:

    1st call: a single function_call for `tool_name`
    2nd call: a final assistant text message

    Returns (fake_client, calls_capture) where calls_capture records each
    create() invocation's kwargs (handy for assertions).
    """
    calls_capture: list[dict] = []

    async def fake_create(**kwargs):
        calls_capture.append(kwargs)
        if len(calls_capture) == 1:
            return SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        call_id=call_id,
                        name=tool_name,
                        arguments=arguments,
                    ),
                ],
                output_text="",
                usage=None,
                status="completed",
                refusal=None,
            )
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    content=[SimpleNamespace(type="output_text", text="recovered")],
                ),
            ],
            output_text="recovered",
            usage=SimpleNamespace(
                total_tokens=10, input_tokens=5, output_tokens=5
            ),
            status="completed",
            refusal=None,
        )

    fake_client = SimpleNamespace(
        responses=SimpleNamespace(create=fake_create)
    )
    return fake_client, calls_capture


@pytest.fixture
def fake_openai_client(monkeypatch):
    """Patch OpenAIService.client to bypass the enabled/api_key check.

    Returns a factory: pass it the tool_name you want the LLM to "call",
    get back (service_ready_to_call, fake_client).
    """

    def install(*, tool_name: str, call_id: str = "call-1", arguments: str = "{}"):
        fake_client, _ = _make_fake_client(
            tool_name=tool_name, call_id=call_id, arguments=arguments
        )
        monkeypatch.setattr(
            OpenAIService, "client", property(lambda self: fake_client)
        )
        return fake_client

    return install


async def test_tool_handler_exception_logged_with_context(ctx, fake_openai_client, caplog):
    """When a tool handler raises, an ERROR record with full triage context
    and a traceback is emitted."""
    fake_openai_client(tool_name="boom_tool", call_id="call-xyz",
                       arguments='{"q": "dangerous input"}')

    async def boom_handler(**kwargs):
        raise ValueError("kaboom from handler")

    svc = OpenAIService(ctx)
    with caplog.at_level("ERROR", logger="bob_server.services.openai_service"):
        result = await svc.chat_with_tools(
            messages=[{"role": "user", "content": "trigger the boom"}],
            tools=[],
            tool_handlers={"boom_tool": boom_handler},
            dispatch_id="dispatch-123",
            session_key="sess-abc",
            log_id="log-9",
        )

    # The LLM "recovered" after seeing the error result.
    assert result == "recovered"

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    targets = [r for r in error_records if "Tool call failed" in r.getMessage()]
    assert targets, f"no 'Tool call failed' ERROR log; got {[r.getMessage() for r in error_records]}"

    rec = targets[0]
    msg = rec.getMessage()
    assert "boom_tool" in msg
    assert "call-xyz" in msg
    assert "dispatch-123" in msg
    assert "sess-abc" in msg
    assert "log-9" in msg
    assert "kaboom from handler" in msg
    assert "dangerous input" in msg  # truncated args surfaced
    # iteration is 0-indexed; first tool call happens on iteration 0
    assert "iteration=0" in msg

    # exc_info attached → traceback rendered below the log line in journalctl
    assert rec.exc_info is not None
    assert rec.exc_info[0] is ValueError


async def test_unknown_tool_logged_at_error(ctx, fake_openai_client, caplog):
    """When the LLM hallucinates a tool name not in tool_handlers, an ERROR
    record is emitted with the tool name and call_id."""
    fake_openai_client(tool_name="ghost_tool", call_id="call-ghost")

    svc = OpenAIService(ctx)
    with caplog.at_level("ERROR", logger="bob_server.services.openai_service"):
        result = await svc.chat_with_tools(
            messages=[{"role": "user", "content": "call the ghost"}],
            tools=[],
            tool_handlers={},  # no tools registered → ghost_tool is unknown
            dispatch_id="dispatch-ghost",
            session_key="sess-spooky",
            log_id="log-ghost",
        )

    assert result == "recovered"

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    targets = [r for r in error_records if "Unknown tool requested" in r.getMessage()]
    assert targets, f"no 'Unknown tool requested' log; got {[r.getMessage() for r in error_records]}"

    msg = targets[0].getMessage()
    assert "ghost_tool" in msg
    assert "call-ghost" in msg
    assert "dispatch-ghost" in msg
    assert "sess-spooky" in msg
    assert "log-ghost" in msg
    assert "iteration=0" in msg


async def test_tool_args_truncated_in_log(ctx, fake_openai_client, caplog):
    """Tool args larger than 500 chars are truncated so journalctl isn't flooded."""
    big_arg = "x" * 2000
    fake_openai_client(
        tool_name="big_tool",
        call_id="call-big",
        arguments=f'{{"blob": "{big_arg}"}}',
    )

    async def big_handler(**kwargs):
        raise RuntimeError("fail")

    svc = OpenAIService(ctx)
    with caplog.at_level("ERROR", logger="bob_server.services.openai_service"):
        await svc.chat_with_tools(
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            tool_handlers={"big_tool": big_handler},
            dispatch_id="d", session_key="s", log_id="l",
        )

    targets = [r for r in caplog.records if r.levelname == "ERROR" and "Tool call failed" in r.getMessage()]
    assert targets
    msg = targets[0].getMessage()
    # args= prefix + 500-char payload should be present; the full 2000-char
    # blob must NOT survive intact.
    assert "args=" in msg
    assert big_arg not in msg
