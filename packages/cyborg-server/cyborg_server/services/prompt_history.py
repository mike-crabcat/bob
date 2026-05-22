"""Shared helpers for logging prompts sent to LLM sessions."""

from __future__ import annotations

import logging
from uuid import uuid4

from cyborg_server.database import Database

logger = logging.getLogger(__name__)

PROMPT_CATEGORIES = frozenset({
    "plan_generation",
    "criteria_evaluation",
    "strategy_refinement",
    "learning_extraction",
    "task_planning",
    "health_analysis",
    "follow_up_generation",
    "task_assignment",
    "needs_input",
    "next_action",
    "notification",
    "submission_review",
    "task_retry",
    "task_tap",
    "task_input_response",
    "source_discovery",
    "email_agenda",
    "email_context",
    "email_incoming",
    "email_outgoing",
})


def estimate_token_count(text: str) -> int:
    """Rough token estimate using the ``len // 4`` heuristic."""
    return len(text) // 4


async def log_prompt(
    db: Database,
    *,
    category: str,
    prompt_text: str,
    project_id: str | None = None,
    task_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """INSERT a row into ``prompt_history``, silently swallowing errors."""
    if category not in PROMPT_CATEGORIES:
        logger.warning("Unknown prompt category %r — skipping log", category)
        return

    try:
        await db.execute(
            """
            INSERT INTO prompt_history
                (id, category, prompt_text, project_id, task_id, session_key, token_count_estimate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                category,
                prompt_text,
                project_id,
                task_id,
                session_key,
                estimate_token_count(prompt_text),
            ),
        )
    except Exception:
        logger.debug("Failed to log prompt to prompt_history", exc_info=True)
