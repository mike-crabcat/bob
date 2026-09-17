"""Parser tests for scripts/import_whatsapp_export.py.

The parser is the risky half of the AI doom history backfill (2026-09-14):
the export mixes bidi control characters, false-header lines inside
multi-line messages, and attachment/caption hybrids. These lock down the
shape the import stage relies on. The DB-touching stages are not covered
here — they ran once against a report that was eyeballed first.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_whatsapp_export.py"
_spec = importlib.util.spec_from_file_location("import_whatsapp_export", _SCRIPT)
wim = importlib.util.module_from_spec(_spec)
sys.modules["import_whatsapp_export"] = wim  # dataclass needs it resolvable
_spec.loader.exec_module(wim)

MEDIA = {"00000015-PHOTO-2026-02-06-12-55-54.jpg": 28166}


def parse(text: str, media: dict | None = None):
    return wim.parse_chat(text, media or {})


def test_lrm_line_prefix_and_rtl_inside_header():
    # RLM before "pm", LRM before the body — both must vanish.
    text = "‎[6/2/2026, 12:32:30 p‏m]‎ Mike: Hi Bob, chat openly in this group"
    result = parse(text)
    assert len(result.messages) == 1
    assert result.messages[0].sender_name == "Mike"
    assert result.messages[0].content == "Hi Bob, chat openly in this group"


def test_narrow_no_break_space_before_meridiem():
    text = "[12/4/2026, 2:23:05 pm] Mike: Can you download and install it?"
    result = parse(text)
    assert len(result.messages) == 1
    assert not result.anomalies


def test_false_headers_stay_continuation_lines():
    # Marvin-quote subtitle lines and ASCII-art rows start with '[' but are
    # not headers — they must join the previous message's body.
    text = (
        "[24/2/2026, 6:34:42 am] Bob Bot: Marvin moments\n"
        "[01:40] \"...terrible pain in all the diodes down my left side...\"\n"
        "[02:00] \"Life, don't talk to me about life...\"\n"
        "[Row 1] Grenade assemblers (5x) feeding\n"
        "[25/2/2026, 8:00:00 am] Mike: anyway\n"
    )
    result = parse(text)
    assert len(result.messages) == 2
    assert "[01:40]" in result.messages[0].content
    assert "[Row 1]" in result.messages[0].content
    assert result.messages[1].content == "anyway"


def test_multiline_body_joined_with_newlines():
    text = "[6/2/2026, 12:43:04 pm] Bob Bot: line one\n\nline two\nline three"
    result = parse(text)
    assert result.messages[0].content == "line one\n\nline two\nline three"


def test_caption_plus_attachment_split():
    text = "[6/2/2026, 12:55:54 pm] Bob Bot: Static image version <attached: 00000015-PHOTO-2026-02-06-12-55-54.jpg>"
    result = parse(text, MEDIA)
    msg = result.messages[0]
    assert msg.content == "Static image version"
    assert msg.media["zip_name"] == "00000015-PHOTO-2026-02-06-12-55-54.jpg"
    assert msg.media["kind"] == "image"
    assert not result.anomalies


def test_attachment_only_gets_bridge_stub_content():
    text = "[6/2/2026, 1:34:52 pm] Mike: <attached: 00000015-PHOTO-2026-02-06-12-55-54.jpg>"
    msg = parse(text, MEDIA).messages[0]
    assert msg.content == "[Image]"
    assert msg.media["kind"] == "image"


def test_media_kind_classification():
    assert wim.classify_media("00000323-GIF-2026-02-07-15-17-18.mp4") == ("video", True)
    assert wim.classify_media("00004588-VIDEO-2026-04-19-19-01-02.mp4") == ("video", False)
    assert wim.classify_media("00000455-AUDIO-2026-02-08-12-39-17.opus") == ("audio", False)
    assert wim.classify_media("00005315-AI_Doom_Group_Character_Bible_compressed.pdf")[0] == "document"
    assert wim.classify_media("00000152-file.xml")[0] == "document"
    assert wim.classify_media("00000164-STICKER-2026-02-07-10-24-35.webp") == ("image", False)


def test_system_join_and_deleted_lines_skipped():
    text = (
        "[6/2/2026, 12:32:30 pm] AI doom: ‎You created group “AI doom”\n"
        "[6/2/2026, 12:32:30 pm] AI doom: Messages and calls are end-to-end encrypted.\n"
        "[7/2/2026, 11:56:11 am] Chris: You added Chris\n"
        "[21/4/2026, 6:24:26 pm] Bob Bot: You added Bob Bot\n"
        "[28/2/2026, 12:06:16 pm] David Shedden: This message was deleted.\n"
        "[6/2/2026, 12:32:55 pm] Mike: real message\n"
    )
    result = parse(text)
    assert [m.content for m in result.messages] == ["real message"]
    assert result.skipped["system:AI doom"] == 2
    assert result.skipped["join_event"] == 2
    assert result.skipped["deleted"] == 1


def test_sender_map_roles_and_contacts():
    humans = parse("[6/2/2026, 12:32:55 pm] Mike: hi").messages[0]
    old_bot = parse("[6/2/2026, 12:43:04 pm] Bob Bot: Greetings").messages[0]
    andy = parse("[20/2/2026, 8:00:00 am] Andy Cooksey: hello").messages[0]
    assert (humans.role, humans.sender_id) == ("user", "7c9f0fd7-6134-4495-aa8c-f04f11bc15e8")
    assert (old_bot.role, old_bot.sender_id) == ("assistant", None)  # the old bot is Bob
    # Export "Andy" maps to contact Andrew Cooksey.
    assert andy.sender_id == "83754471-e4f6-4aac-99a6-a3687cc90526"


def test_perth_to_utc_conversion():
    # 6 Feb 2026 12:32:30 pm AWST (UTC+8, no DST) -> 04:32:30 UTC.
    msg = parse("[6/2/2026, 12:32:30 pm] Mike: hi").messages[0]
    assert msg.ts_utc == "2026-02-06 04:32:30"
    # Midnight and noon edges.
    assert parse("[9/2/2026, 12:00:01 am] Mike: a").messages[0].ts_utc == "2026-02-08 16:00:01"
    assert parse("[9/2/2026, 12:59:59 pm] Mike: b").messages[0].ts_utc == "2026-02-09 04:59:59"


def test_unknown_sender_recorded_as_anomaly_not_message():
    text = "[6/2/2026, 12:32:55 pm] Mystery Person: who am I\nfollow-up line"
    result = parse(text)
    assert not result.messages
    assert len(result.anomalies) == 1
    assert "Mystery Person" in result.anomalies[0]


def test_missing_attachment_flagged():
    text = "[6/2/2026, 1:00:00 pm] Mike: <attached: 00009999-PHOTO-2026-02-06-13-00-00.jpg>"
    result = parse(text, MEDIA)
    assert len(result.anomalies) == 1
    assert "missing from zip" in result.anomalies[0]


def test_media_timestamp_crosscheck_detects_timezone_bug():
    # Filename says 13:34:52, message header says 12:55:54 — a 39-minute
    # gap reads as a forward/suspect, and a systematic tz bug would flag
    # every line rather than none.
    text = "[6/2/2026, 12:55:54 pm] Mike: <attached: 00000049-PHOTO-2026-02-06-13-34-52.jpg>"
    result = parse(text, {"00000049-PHOTO-2026-02-06-13-34-52.jpg": 1})
    assert len(result.tz_mismatches) == 1

    ok = parse("[6/2/2026, 1:34:52 pm] Mike: <attached: 00000049-PHOTO-2026-02-06-13-34-52.jpg>",
               {"00000049-PHOTO-2026-02-06-13-34-52.jpg": 1})
    assert not ok.tz_mismatches


def test_jsonl_roundtrip_shape():
    msg = parse("[6/2/2026, 12:55:54 pm] Bob Bot: Static image version "
                "<attached: 00000015-PHOTO-2026-02-06-12-55-54.jpg>", MEDIA).messages[0]
    row = wim.msg_to_jsonl(msg)
    json.dumps(row)  # serialisable
    assert row["role"] == "assistant"
    assert row["media"]["kind"] == "image"
    assert row["media"]["mime"] == "image/jpeg"
    # Deterministic ids: a re-parse produces the same trace handle.
    again = wim.msg_to_jsonl(msg)
    assert row["message_id"] == again["message_id"]
