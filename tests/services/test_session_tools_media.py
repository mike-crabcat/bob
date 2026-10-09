"""History tools carry the media pointer the chat history has (2026-10-09:
a captioned photo paged back via get_session_messages read as plain text,
so Bob decided he couldn't see it)."""

import json

from server.services.session_tools import _media_note


def test_media_note_points_at_existing_image(tmp_path):
    img = tmp_path / "beer.jpg"
    img.write_bytes(b"x")
    note = _media_note(json.dumps({"image_path": str(img)}))
    assert str(img) in note and "read_image" in note


def test_media_note_silent_for_missing_or_plain(tmp_path):
    assert _media_note(json.dumps({"image_path": str(tmp_path / "gone.jpg")})) == ""
    assert _media_note(None) == ""
    assert _media_note("not json") == ""
    assert _media_note(json.dumps({"wa_message_id": "x"})) == ""
