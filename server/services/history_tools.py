"""Record discipline — the prompt layers that make Bob search before
asserting about the past (2026-09-18).

Motivating incident (Weeming Boys, Freo v Sydney): Bob tipped "Dockers by
2 goals" pre-game; 145 messages later the tip sat outside the 100-row turn
window, and the turn said "I've got no record of a pre-game tip". The
search capability already existed (``search_session_messages`` in
``session_tools.py`` — cross-session, full history) and was attached to
that turn: the failure was that the model didn't *think* to search, not
that it couldn't. A first fix shipped a duplicate current-session-only
tool here; consolidated away the same day when the DM steering incident
showed the model habitually using ``search_session_messages`` — one tool,
one name, one habit.

This module now owns the encouragement layers only:
- ``HISTORY_DISCIPLINE_NOTE`` — the standing constraint, injected wherever
  chat turns assemble their system prompt (Mike's ruling: for ALL
  responses — an unverified claim about the past is worse than admitting
  you need to look).
- ``past_reference_note()`` — the just-in-time amplifier for inbound
  messages that challenge what was said ("you predicted…"), where the pull
  toward confident denial is strongest. Live observation: with only the
  standing note, the serving model NARRATES unrun searches — the JIT note
  and the anti-narration clause are the counterweights.
"""

from __future__ import annotations

import re

HISTORY_DISCIPLINE_NOTE = (
    "## Record discipline\n"
    "Before asserting anything about what was said earlier — including "
    "claiming \"I have no record of that\" — run search_session_messages "
    "and check. An unverified claim about the past is worse than saying "
    "you need to look; never deny having said something unsearched, and "
    "never DESCRIBE a search you did not run — the tool call is visible "
    "in the transcript, so narrating one you skipped is a lie on the "
    "record.")

# Second-person past-reference shapes — the confrontation class where the
# model is most tempted to double down instead of looking (Freo-tip shape).
_PAST_REFERENCE_RE = re.compile(
    r"\b(you\s+(said|told|predicted|promised|mentioned|claimed|posted|sent|"
    r"asked|tipped|suggested)|didn'?t\s+you|your\s+(original|earlier|"
    r"previous|tip|prediction|promise|prediction\s+then)|changed\s+your|"
    r"remember\s+(when|what|that)|what\s+did\s+you\s+(say|tell|tip)|"
    r"you\s+already\s+said)\b",
    re.IGNORECASE)


def past_reference_note(inbound_text: str) -> str:
    """The JIT amplifier: a system note appended when the inbound message
    references earlier conversation. False positives cost one ignorable
    line; a false negative is the incident this module exists for."""
    if inbound_text and _PAST_REFERENCE_RE.search(inbound_text):
        return ("[Record check] This message is about something said "
                "earlier. Run search_session_messages BEFORE answering — "
                "assert nothing about the past, including \"no record\", "
                "unverified.")
    return ""
