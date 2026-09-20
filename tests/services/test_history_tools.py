"""Record discipline (2026-09-18 Freo-tip incident) — the prompt layers:

- past_reference_note: fires on the confrontation shapes, silent on
  ordinary chatter; names the EXISTING search tool (consolidated — a
  duplicate current-session-only tool was shipped first and removed the
  same day; one tool, one name, one habit)
- HISTORY_DISCIPLINE_NOTE: names the tool and forbids narrating unrun
  searches (live observation: the model does exactly that with only a
  standing note)
"""

from __future__ import annotations

from server.services.history_tools import (
    HISTORY_DISCIPLINE_NOTE,
    past_reference_note,
)


async def test_past_reference_note_triggers_on_confrontation(ctx):
    fires = [
        "Ok but before the game started you predicted Freo would win",
        "So you've changed your original prediction then",
        "didn't you say you'd handle it?",
        "Remember what you promised?",
        "what did you tell Simon",
    ]
    for text in fires:
        assert past_reference_note(text), text
    quiet = [
        "Anyone watching the game tonight?",
        "Earlier I had lunch at the pub",
        "Dockers by 30 I reckon",
    ]
    for text in quiet:
        assert not past_reference_note(text), text


def test_discipline_note_names_the_tool():
    assert "search_session_messages" in HISTORY_DISCIPLINE_NOTE
    assert "no record" in HISTORY_DISCIPLINE_NOTE.lower()
    # Anti-narration clause (the model narrates unrun searches without it).
    assert "did not run" in HISTORY_DISCIPLINE_NOTE
