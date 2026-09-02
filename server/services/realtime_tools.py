"""Vetted tool set available to OpenAI Realtime voice agents during a call.

These are deliberately minimal and task-agnostic — the voice agent runs
autonomously on a phone call, so we expose a small curated surface rather than
Bob's full tool set. The bridge registers these on the Realtime session via
``Tool.to_openai_format()`` and dispatches ``response.function_call_arguments``
events to the handlers. When the agent calls ``end_call``, the bridge winds
the session down itself (see ``_handle_function_call``).

The outcome tools (``report_success`` / ``report_failure``) are generic: Bob's
prompt tells the agent what facts to capture in ``details`` for the specific
task (booking time, appointment ID, confirmation number — whatever matters).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from server.services.tools import tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


def make_realtime_tools(
    ctx: AppContext,
    *,
    phone_number: str = "",
) -> list:
    """Build the voice-agent tool set for a Realtime call.

    ``phone_number`` is the number being called (used by ``get_caller_details``).
    """

    @tool
    async def end_call() -> str:
        """End the phone call now. Call this once the task is complete or the other party is done."""
        return json.dumps({"ended": True})

    @tool
    async def get_caller_details() -> str:
        """Look up the contact record for the number being called (name, notes)."""
        if not phone_number:
            return json.dumps({"error": "no phone number associated with this call"})
        from server.repositories.contacts import ContactRepository
        row = await ContactRepository(ctx.db).get_by_phone(phone_number)
        if row is None:
            return json.dumps({"found": False})
        return json.dumps({"found": True, "name": row["name"], "contact_id": row["id"]})

    @tool
    async def report_success(summary: str, details: str = "") -> str:
        """Report that the call's task was achieved. Call once the goal is done.

        - summary: one line stating the outcome, e.g. "Booked Trattoria Mario for 4 at 7:30pm Thursday"
        - details: the key facts to capture, as specified by your instructions for this task.
          Put each fact on its own "key: value" line (e.g. "time: 7:30pm\\nparty_size: 4\\nreference: JC-4421").
          Only include facts your instructions asked for.
        """
        return json.dumps({"status": "success", "summary": summary, "details": details})

    @tool
    async def report_failure(reason: str, details: str = "") -> str:
        """Report that the task could not be completed. Call if the goal was not achieved.

        - reason: short description of what blocked you, e.g.
          "fully booked", "required a credit card to hold", "voicemail, no human answer".
        - details: any partial information captured before the blocker (same "key: value" line format).
        """
        return json.dumps({"status": "failure", "reason": reason, "details": details})

    return [end_call, get_caller_details, report_success, report_failure]

