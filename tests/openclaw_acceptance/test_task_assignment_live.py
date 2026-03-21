from __future__ import annotations

import time
from typing import Any

import pytest


pytestmark = pytest.mark.openclaw_live


def _skip_if_live_backend_unavailable(exc: BaseException) -> None:
    message = str(exc).lower()
    if any(fragment in message for fragment in ("429", "rate limit", "usage limit reached", "quota")):
        pytest.skip(f"OpenClaw model backend is currently unavailable: {exc}")
    if "gateway" in message and "timed out" in message:
        pytest.skip(f"OpenClaw gateway timed out during acceptance test: {exc}")


def _assert_no_internal_leakage(text: str) -> None:
    lowered = text.lower()
    assert "task id" not in lowered
    assert "notification id" not in lowered
    assert "session key" not in lowered
    assert "cyborg task assignment" not in lowered


def _start_task_assignment_session(
    acceptance_builder: Any,
    live_openclaw: Any,
    *,
    task_id: str,
    cyborg_service_url: str,
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    notification = acceptance_builder.get_task_assignment_notification(task_id)
    route, session_key = live_openclaw.resolve_target_route(notification)
    hook_service = live_openclaw.make_hook_service(cyborg_service_url=cyborg_service_url)
    params = hook_service._build_task_assignment_agent_params(notification, route, session_key)
    params["deliver"] = False
    params.pop("channel", None)
    params.pop("to", None)
    response = live_openclaw.call_gateway(
        "agent",
        params,
        expect_final=True,
        timeout_seconds=90.0,
        cyborg_service_url=cyborg_service_url,
    )
    text = live_openclaw.response_text(response, session_key=session_key)
    history = live_openclaw.fetch_history(session_key)
    if not text:
        text, history = live_openclaw.wait_for_assistant_reply(session_key, previous_assistant_count=0, timeout_seconds=90.0)
    live_openclaw.write_artifact("task_assignment_notification.json", notification)
    live_openclaw.write_artifact("task_assignment_route.json", route)
    live_openclaw.write_artifact("task_assignment_history_initial.json", history)
    return notification, session_key, text, history


def _wait_for_task_completion(
    acceptance_builder: Any,
    live_openclaw: Any,
    *,
    task_id: str,
    session_key: str,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_task = acceptance_builder.get_task(task_id)
    while time.monotonic() < deadline:
        task = acceptance_builder.get_task(task_id)
        last_task = task
        if task["status"] == "completed":
            return task
        if task["status"] == "failed":
            raise AssertionError(f"Task {task_id} failed instead of completing: {task.get('result')}")
        time.sleep(2.0)
    history = live_openclaw.fetch_history(session_key)
    notifications = acceptance_builder.list_notifications()
    live_openclaw.write_artifact("task_assignment_completion_timeout_history.json", history)
    live_openclaw.write_artifact("task_assignment_completion_timeout_task.json", last_task)
    live_openclaw.write_artifact("task_assignment_completion_timeout_notifications.json", notifications)
    history_text = "\n".join(
        text
        for text in (
            "\n".join(part.get("text", "") for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "text")
            if isinstance(message.get("content"), list)
            else str(message.get("content", "")).strip()
            for message in history.get("messages", [])
            if message.get("role") == "assistant"
        )
        if text.strip()
    )
    if any(fragment in history_text.lower() for fragment in ("can't", "cannot", "unable", "don’t have access", "don't have access", "no tool")):
        raise AssertionError(
            "OpenClaw did not complete the task, and the session history suggests the Cyborg skill/tooling is unavailable. "
            f"Last task state: {last_task}"
        )
    raise AssertionError(f"Timed out waiting for task {task_id} to complete. Last task state: {last_task}")


def _find_task_result_notification(acceptance_builder: Any, *, task_id: str) -> dict[str, Any]:
    notifications = acceptance_builder.list_notifications()
    for notification in notifications:
        if notification["entity_id"] == task_id and notification["notification_type"] == "task_result":
            return notification
    raise AssertionError(f"No task_result notification found for task {task_id}")


def test_live_task_assignment_direct_answer_completes_task(
    acceptance_builder: Any,
    live_openclaw: Any,
    cyborg_http_server: str,
) -> None:
    contact = acceptance_builder.create_contact(
        name="Acceptance Contact",
        phone_number="+61456224867",
        metadata={"acceptance": True},
    )
    task = acceptance_builder.create_task(
        title="Ask for favourite fruit",
        description="Ask Mike what his favourite fruit is, then complete the task with only the fruit he names.",
        plan="Ask the contact for their favourite fruit. When they answer clearly, complete the task with the exact fruit.",
        requested_by="OpenClaw acceptance",
        metadata={
            "channel": "whatsapp",
            "session_key": "whatsappgroup-acceptance-source",
            "chat_id": "120363426096069246@g.us",
            "target_session": {
                "channel": "whatsapp",
                "kind": "dm",
                "contact_id": contact["id"],
            },
        },
        approve_plan=True,
    )

    try:
        _, session_key, first_reply, history = _start_task_assignment_session(
            acceptance_builder,
            live_openclaw,
            task_id=task["id"],
            cyborg_service_url=cyborg_http_server,
        )
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    _assert_no_internal_leakage(first_reply)
    assert first_reply.strip()
    initial_assistant_count = live_openclaw.assistant_message_count(history)

    live_openclaw.send_chat(session_key, "My favourite fruit is mango.")
    completed_task = _wait_for_task_completion(
        acceptance_builder,
        live_openclaw,
        task_id=task["id"],
        session_key=session_key,
    )

    assert completed_task["status"] == "completed"
    assert "mango" in str(completed_task.get("result", "")).lower()

    result_notification = _find_task_result_notification(acceptance_builder, task_id=task["id"])
    assert "mango" in result_notification["message"].lower()

    updated_history = live_openclaw.fetch_history(session_key)
    assert live_openclaw.assistant_message_count(updated_history) >= initial_assistant_count


def test_live_task_assignment_requests_follow_up_before_completion(
    acceptance_builder: Any,
    live_openclaw: Any,
    cyborg_http_server: str,
) -> None:
    contact = acceptance_builder.create_contact(
        name="Acceptance Follow-up Contact",
        phone_number="+61456224867",
        metadata={"acceptance": True},
    )
    task = acceptance_builder.create_task(
        title="Ask for favourite movie and reason",
        description=(
            "Ask Mike for his favourite movie and why it is his favourite. "
            "The task is only complete once both the movie and the reason are captured."
        ),
        plan=(
            "Ask for the favourite movie and the reason it stands out. "
            "If the first answer only gives the movie, ask one follow-up question for the reason. "
            "Complete the task only after both details are clear."
        ),
        requested_by="OpenClaw acceptance",
        metadata={
            "channel": "whatsapp",
            "session_key": "whatsappgroup-acceptance-source",
            "chat_id": "120363426096069246@g.us",
            "target_session": {
                "channel": "whatsapp",
                "kind": "dm",
                "contact_id": contact["id"],
            },
        },
        approve_plan=True,
    )

    try:
        _, session_key, first_reply, history = _start_task_assignment_session(
            acceptance_builder,
            live_openclaw,
            task_id=task["id"],
            cyborg_service_url=cyborg_http_server,
        )
    except Exception as exc:
        _skip_if_live_backend_unavailable(exc)
        raise

    _assert_no_internal_leakage(first_reply)
    assert first_reply.strip()
    initial_assistant_count = live_openclaw.assistant_message_count(history)

    live_openclaw.send_chat(session_key, "Inception.")
    follow_up_text, _ = live_openclaw.wait_for_assistant_reply(
        session_key,
        previous_assistant_count=initial_assistant_count,
        timeout_seconds=90.0,
    )
    follow_up_lower = follow_up_text.lower()
    assert "why" in follow_up_lower or "what do you like" in follow_up_lower or "reason" in follow_up_lower

    mid_task = acceptance_builder.get_task(task["id"])
    assert mid_task["status"] != "completed"

    live_openclaw.send_chat(session_key, "Because the layered dream structure is clever and memorable.")
    completed_task = _wait_for_task_completion(
        acceptance_builder,
        live_openclaw,
        task_id=task["id"],
        session_key=session_key,
    )

    result_text = str(completed_task.get("result", "")).lower()
    assert "inception" in result_text
    assert "dream" in result_text or "clever" in result_text or "memorable" in result_text

    result_notification = _find_task_result_notification(acceptance_builder, task_id=task["id"])
    assert "inception" in result_notification["message"].lower()
