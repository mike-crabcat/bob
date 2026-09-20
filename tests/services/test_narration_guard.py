"""Narration guard (2026-09-19 gnome-repair incident) — the send rescue must
never launder claimed-but-unmade tool calls into the chat:

- detection: "[tools used: …]", "(request_id=…)", '→ {"ok": true…}' in reply
  text are fabrications by construction — the platform renders real calls in
  tool blocks, never in the reply
- stripping: the fabricated block is removed; the real prose survives
- the runner retries once with an explicit correction before delivering
  stripped prose as a last resort
"""

from __future__ import annotations

from server.services.dispatch_runner import (
    _narrated_tool_calls,
    _strip_narration,
)

_HALLUCINATION = (
    '[tools used: send_whatsapp_message(text=Fair — repair-then-paint is the '
    'right order…) → Message sent (request_id=88ff8c14-0b7b-4e2e-bd7b-8f8a-449c5b417a90); '
    'create_subagent(agent_type=script, task=python scratch/gnome_paint_3mf_v3.py '
    '--repair-first…) → {"ok": true, "subagent_id": "38d9c947-…[truncated])]\n\n'
    "Fair — repair-then-paint is the right order; painting a holey mesh is "
    "sealing a leaking boat with bunting. Re-running: watertight repair on "
    "the v3 mesh first, then the colour pass and fresh 3MF."
)

_CLEAN = ("Fair — repair-then-paint is the right order. Re-running the "
          "repair first, then the colour pass.")


def test_detection_matches_the_incident_shapes():
    assert _narrated_tool_calls(_HALLUCINATION)
    assert _narrated_tool_calls('did a send_whatsapp_message → {"ok": true, "x": 1}')
    assert _narrated_tool_calls("Message sent (request_id=88ff8c14-0b7b)")
    assert _narrated_tool_calls("[Tools Used: bash(ls)] something")


def test_detection_ignores_ordinary_prose():
    assert not _narrated_tool_calls(_CLEAN)
    assert not _narrated_tool_calls("I'll run the repair first — tools coming.")
    assert not _narrated_tool_calls("")
    assert not _narrated_tool_calls(None)
    # A request id mentioned as data (short/non-uuid) is not a narration.
    assert not _narrated_tool_calls("see request id 1234 in the log")


def test_strip_removes_fabrication_keeps_prose():
    stripped = _strip_narration(_HALLUCINATION)
    assert "repair-then-paint" in stripped          # prose survives
    assert "tools used" not in stripped.lower()
    assert "request_id" not in stripped
    assert "subagent_id" not in stripped
    # All-narration text falls back to the original rather than emptying.
    only = "[tools used: send_whatsapp_message(x) → Message sent (request_id=aaaaaaaa-bbbb)]"
    assert _strip_narration(only) == only
