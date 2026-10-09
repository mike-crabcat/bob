"""bg job exit wakes speak in human chats (2026-10-09: the hat mockup reply
was dropped as a wake_nudge)."""

from unittest.mock import AsyncMock, patch

from server.services import process_tools as pt


def _row(parent):
    return {"id": 1, "name": "doom-logo", "exit_code": 0, "systemd_result": "success",
            "parent_session_key": parent, "log": None}


async def test_human_chat_exit_wake_is_a_relay(ctx):
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)) as wake:
        await pt._deliver_exit_wake(ctx, _row("agent:main:whatsapp:dm:61456224867"))
    assert wake.await_args.kwargs["provenance"] == "task_relay"
    assert "POSTED to this chat" in wake.await_args.args[2]


async def test_goal_room_exit_wake_stays_quiet(ctx):
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)) as wake:
        await pt._deliver_exit_wake(ctx, _row("agent:goal-x:utility"))
    assert wake.await_args.kwargs["provenance"] == "wake_nudge"
