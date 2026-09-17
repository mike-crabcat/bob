#!/usr/bin/env python3
"""Import a WhatsApp "Export chat" zip into an existing group conversation.

One-off backfill tool for the AI doom history import (2026-09-14): the
rebuilt Bob's DB starts 2026-05-10, this folds the Feb–Apr 2026 export of
the old group (old-Bot era) in ahead of it so session search and paged
history reads reach back to the group's creation.

Stages — parse first, review the report, then import:

    parse   <zip> <staging_dir>
            Read _chat.txt out of the zip, parse to <staging_dir>/messages.jsonl
            and write report.json. No DB access.
    import  <staging_dir> <zip>
            Copy media into the workspace media dirs and insert the rows in a
            single transaction. Refuses if anomalies were recorded, if a
            previous import is already present, or if the conversation or any
            mapped contact is missing.

Conventions mirrored from the live bridge (whatsapp_bridge_service):
  * images/videos land in workspace/whatsapp-media/<name> with metadata keys
    image_path / video_path / is_gif and content stubs [Image]/[Video]/[GIF]
    — the prompt assembler turns those into read_image stubs on replay.
  * documents land in workspace/whatsapp_media/<short-id>_<safe-name> with
    document_path + document_workspace_path + document_filename
    (mirrors _copy_document_to_workspace).
  * voice notes: the bridge has no audio shape yet, so the file lands in
    whatsapp-media with an audio_path metadata key (invented here, nothing
    reads it yet) and a '[Voice note]' content stub — ready for a
    transcription backfill to replace the stub with text.

Imported rows: role user (humans) / assistant (the old bot), sender_id =
contacts.id via the curated name map below, channel whatsapp, dispatched=1
(history, not pending stimulus — the patience gate reads dispatched=0 as
"someone is waiting for a reply"), provenance 'whatsapp_import' (replays as
plain dialogue — it's in no replay-exclusion list — and gives a one-line
rollback: DELETE FROM messages WHERE provenance = 'whatsapp_import').
created_at is the Perth-local export time converted to naive UTC, matching
every other row in the table.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PERTH = ZoneInfo("Australia/Perth")  # UTC+8, no DST in the export window
PROVENANCE = "whatsapp_import"

# Curated for the "AI doom" export: the txt format carries display names
# only (no phone numbers), so the map is the one place a wrong-person
# mapping could creep in. Export name -> (role, contact_id or None).
SENDER_MAP: dict[str, tuple[str, str | None]] = {
    "Bob Bot": ("assistant", None),  # the old bot — Bob's own messages
    "Mike": ("user", "7c9f0fd7-6134-4495-aa8c-f04f11bc15e8"),  # Mike Cleaver
    "David Shedden": ("user", "cdda1eb1-82ed-4bac-9123-a6a8032dda7f"),
    "Sylvain Ayrault": ("user", "b326ecae-8cb0-4f0a-8826-3d1888ff0fb8"),  # contact "Sylvain"
    "Rupert Quekett": ("user", "62ad03f6-3632-4a22-94a0-cd058dc6fadd"),
    "Andy Cooksey": ("user", "83754471-e4f6-4aac-99a6-a3687cc90526"),  # contact "Andrew Cooksey"
    "Chris": ("user", "46b1eacc-ab32-4acf-aea2-f778cc9a2b51"),
    # Group system banner lines ("You created group", encryption notice…).
    # Skipped, counted in the report; they are not dialogue.
    "AI doom": ("skip", "system"),
}

CONVERSATION_ID = "agent:main:whatsapp:group:120363422982048691"
GROUP_LABEL = "AI doom"

# Strict full-date header: anything looser (Marvin-quote "[01:40] …"
# subtitle lines, "[Row 1]" art inside multi-line messages) must stay a
# continuation line. \s covers the narrow no-break space WhatsApp puts
# before am/pm in some lines (U+202F), but normalise() replaces it anyway.
HEADER_RE = re.compile(
    r"^\[(\d{1,2})/(\d{1,2})/(\d{4}), (\d{1,2}):(\d{2}):(\d{2})\s*([ap])m\]\s*(.*)$"
)
ATTACH_RE = re.compile(r"\s*<attached:\s+([^>]+)>\s*")
JOIN_EVENT_RE = re.compile(
    r"^You (added|removed|created|left|changed|joined|exited|promoted)"
)
DELETED_TEXT = "This message was deleted."
COUNTER_PREFIX_RE = re.compile(r"^\d{8}-")
MEDIA_TS_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.\w+$")

IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


# ---------------------------------------------------------------- normalise

_BIDI_TRANSLATION = dict.fromkeys(map(ord, "‎‏"))  # LRM / RLM


def normalise(text: str) -> str:
    """Strip directional marks and narrow spaces the export sprinkles
    everywhere — including inside headers ("2:23:05{NNSP}pm")."""
    return (
        text.translate(_BIDI_TRANSLATION)
        .replace(" ", " ")
        .replace(" ", " ")
    )


# ------------------------------------------------------------------- parse

@dataclass
class ParsedMessage:
    index: int  # 1-based header-line number in the export (trace handle)
    ts_utc: str  # naive-UTC 'YYYY-MM-DD HH:MM:SS', the table's convention
    ts_local: str  # Perth wall clock '… AWST', for reports / cross-checks
    role: str  # 'user' | 'assistant'
    sender_id: str | None
    sender_name: str
    content: str
    media: dict | None = None  # zip name + resolved kind/dest/mime


@dataclass
class ParseResult:
    messages: list[ParsedMessage] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    anomalies: list[str] = field(default_factory=list)
    tz_mismatches: list[str] = field(default_factory=list)


def _local_to_utc(day: int, month: int, year: int,
                  hour12: int, minute: int, second: int, meridiem: str) -> datetime:
    hour = hour12 % 12 + (12 if meridiem == "p" else 0)
    local = datetime(year, month, day, hour, minute, second, tzinfo=PERTH)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def classify_media(zip_name: str) -> tuple[str, bool]:
    """(kind, is_gif) from the export's name prefix / extension.

    Kinds: image | video | audio | document. The PDFs carry their title as
    the prefix token (no PHOTO/file marker), so anything unrecognised is a
    document — matching how the bridge treats unknown attachments.
    """
    stem = Path(zip_name).name
    token = stem.split("-", 1)[1] if "-" in stem else ""
    ext = Path(stem).suffix.lower()
    if token.startswith(("PHOTO", "IMG", "STICKER")):
        return "image", False
    if token.startswith("GIF"):
        return "video", True
    if token.startswith(("VIDEO", "VID")):
        return "video", False
    if token.startswith(("AUDIO", "PTT")) or ext in (".opus", ".m4a", ".aac"):
        return "audio", False
    if ext in IMAGE_EXTS:
        return "image", False
    return "document", False


def parse_chat(text: str, media_index: dict[str, int]) -> ParseResult:
    """Parse a normalised _chat.txt into messages + skip/anomaly tallies."""
    result = ParseResult()
    pending: tuple[ParsedMessage, int] | None = None  # (msg, header line no)

    def finish() -> None:
        nonlocal pending
        if pending is None:
            return
        msg, _ = pending
        pending = None
        body = msg.content
        attach_match = ATTACH_RE.search(body)
        if attach_match:
            zip_name = attach_match.group(1).strip()
            msg.content = ATTACH_RE.sub("", body).strip()
            if zip_name not in media_index:
                result.anomalies.append(
                    f"line {msg.index}: attachment {zip_name!r} missing from zip"
                )
            else:
                kind, is_gif = classify_media(zip_name)
                msg.media = {"zip_name": zip_name, "kind": kind, "is_gif": is_gif}
            _set_stub_content(msg)

        skip_reason = _skip_reason(msg.sender_name, msg.content)
        if skip_reason:
            result.skipped[skip_reason] = result.skipped.get(skip_reason, 0) + 1
            return
        if not msg.content and not msg.media:
            return  # empty caption on an unfound attachment — nothing to keep
        result.messages.append(msg)
        _crosscheck_media_ts(msg, result)

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = normalise(raw)
        header = HEADER_RE.match(line)
        if not header:
            if pending is not None:
                pending[0].content += "\n" + line
            continue
        finish()  # previous message ends where this header begins
        day, month, year = int(header[1]), int(header[2]), int(header[3])
        utc = _local_to_utc(day, month, year, int(header[4]),
                            int(header[5]), int(header[6]), header[7])
        rest = header[8]
        sender, sep, body = rest.partition(": ")
        if not sep or sender not in SENDER_MAP:
            result.anomalies.append(
                f"line {lineno}: unresolved sender {sender!r} — line held back"
            )
            pending = None
            continue
        role, contact_id = SENDER_MAP[sender]
        if role == "skip":
            pending = None
            result.skipped[f"system:{sender}"] = result.skipped.get(f"system:{sender}", 0) + 1
            continue
        local = utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(PERTH)
        pending = (ParsedMessage(
            index=lineno,
            ts_utc=utc.strftime("%Y-%m-%d %H:%M:%S"),
            ts_local=local.strftime("%Y-%m-%d %H:%M:%S") + " AWST",
            role=role,
            sender_id=contact_id,
            sender_name=sender,
            content=body,
        ), lineno)
    finish()
    return result


def _skip_reason(sender: str, content: str) -> str | None:
    if JOIN_EVENT_RE.match(content):
        return "join_event"
    if content == DELETED_TEXT:
        return "deleted"
    return None


def _set_stub_content(msg: ParsedMessage) -> None:
    """Attachment-only lines get the bridge's fallback stubs; captions keep
    their text (the attachment rides in metadata, exactly like ingress)."""
    if not msg.media or msg.content:
        return
    kind = msg.media["kind"]
    if msg.media.get("is_gif"):
        msg.content = "[GIF]"
    elif kind == "image":
        msg.content = "[Image]"
    elif kind == "video":
        msg.content = "[Video]"
    elif kind == "audio":
        msg.content = "[Voice note]"
    else:
        msg.content = f"[Document: {document_filename(msg.media['zip_name'])}]"


def document_filename(zip_name: str) -> str:
    """Human filename: strip the export's 8-digit counter prefix."""
    return COUNTER_PREFIX_RE.sub("", Path(zip_name).name) or "document"


def _crosscheck_media_ts(msg: ParsedMessage, result: ParseResult) -> None:
    """WhatsApp media filenames embed the sender's local send time — a free
    timezone validator. Forwarded media legitimately keeps an older stamp,
    so mismatches are reported, not fatal; a systematic tz bug would light
    up nearly every line."""
    if not msg.media:
        return
    m = MEDIA_TS_RE.search(msg.media["zip_name"])
    if not m:
        return
    file_local = datetime(*(int(g) for g in m.groups()))
    msg_local = datetime.strptime(msg.ts_local[:19], "%Y-%m-%d %H:%M:%S")
    delta = abs((file_local - msg_local).total_seconds())
    if delta > 120:
        result.tz_mismatches.append(
            f"line {msg.index}: {msg.media['zip_name']} "
            f"file={file_local:%Y-%m-%d %H:%M:%S} msg={msg_local:%Y-%m-%d %H:%M:%S}"
        )


# ------------------------------------------------------------- report/jsonl

def build_report(result: ParseResult, media_index: dict[str, int], zip_path: Path) -> dict:
    by_sender: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    referenced: set[str] = set()
    for msg in result.messages:
        by_sender[msg.sender_name] = by_sender.get(msg.sender_name, 0) + 1
        if msg.media:
            by_kind[msg.media["kind"]] = by_kind.get(msg.media["kind"], 0) + 1
            referenced.add(msg.media["zip_name"])
    return {
        "zip": str(zip_path),
        "zip_media_files": len(media_index),
        "media_unreferenced": sorted(set(media_index) - referenced),
        "messages": len(result.messages),
        "first_ts": result.messages[0].ts_utc if result.messages else None,
        "last_ts": result.messages[-1].ts_utc if result.messages else None,
        "by_sender": dict(sorted(by_sender.items(), key=lambda kv: -kv[1])),
        "by_media_kind": by_kind,
        "skipped": result.skipped,
        "anomalies": result.anomalies,
        "tz_mismatches": result.tz_mismatches,
    }


def msg_to_jsonl(msg: ParsedMessage) -> dict:
    media = None
    if msg.media:
        media = {**msg.media,
                 "mime": mime_for(msg.media["zip_name"], msg.media["kind"])}
    return {
        "index": msg.index,
        "ts_utc": msg.ts_utc,
        "role": msg.role,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "content": msg.content,
        "media": media,
        "message_id": str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"bob:{PROVENANCE}:{GROUP_LABEL}:{msg.index}")),
    }


def mime_for(zip_name: str, kind: str) -> str:
    ext = Path(zip_name).suffix.lower()
    if kind == "image":
        return IMAGE_EXTS.get(ext, "image/jpeg")
    if kind == "video":
        return "video/mp4"
    if kind == "audio":
        return "audio/opus" if ext == ".opus" else "audio/ogg"
    return "application/octet-stream"


# ------------------------------------------------------------------ stages

def cmd_parse(zip_path: Path, staging: Path) -> int:
    with zipfile.ZipFile(zip_path) as zf:
        chat_names = [n for n in zf.namelist() if n.endswith("_chat.txt")]
        if len(chat_names) != 1:
            print(f"error: expected exactly one _chat.txt, found {chat_names}", file=sys.stderr)
            return 2
        text = zf.read(chat_names[0]).decode("utf-8")
        media_index = {
            n: zf.getinfo(n).file_size
            for n in zf.namelist()
            if not n.endswith("/") and not n.endswith("_chat.txt")
        }

    result = parse_chat(text, media_index)
    report = build_report(result, media_index, zip_path)

    staging.mkdir(parents=True, exist_ok=True)
    (staging / "messages.jsonl").write_text(
        "\n".join(json.dumps(msg_to_jsonl(m), ensure_ascii=False) for m in result.messages) + "\n",
        encoding="utf-8")
    (staging / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {staging/'messages.jsonl'} and {staging/'report.json'}")
    if result.anomalies:
        print(f"\nREFUSING further use: {len(result.anomalies)} anomalies — "
              f"the import stage will not run until these are resolved.", file=sys.stderr)
        return 1
    return 0


def _dest_for(media: dict, workspace: Path) -> tuple[Path, dict]:
    """(destination file, metadata) mirroring the bridge's media layout."""
    kind = media["kind"]
    if kind == "document":
        dest_dir = workspace / "whatsapp_media"
        name = Path(media["zip_name"]).name
        short_id = name.split("-", 1)[0][:8] or uuid.uuid4().hex[:8]
        safe = re.sub(r"[^\w.\-]", "_", document_filename(name)) or "document"
        dest = dest_dir / f"{short_id}_{safe}"
        meta = {
            "document_path": str(dest),
            "document_workspace_path": str(dest.relative_to(workspace)),
            "document_filename": document_filename(name),
        }
    else:
        dest = workspace / "whatsapp-media" / Path(media["zip_name"]).name
        if kind == "image":
            meta = {"image_path": str(dest), "image_mime_type": media["mime"]}
        elif kind == "video":
            meta = {
                "video_path": str(dest),
                "video_mime_type": media["mime"],
                "is_gif": bool(media.get("is_gif")),
            }
        else:  # audio — no bridge shape exists yet; flagged in the header
            meta = {"audio_path": str(dest), "audio_mime_type": media["mime"]}
    return dest, meta


def cmd_import(staging: Path, zip_path: Path, db_path: Path, workspace: Path,
               dry_run: bool = False) -> int:
    report = json.loads((staging / "report.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (staging / "messages.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if report.get("anomalies"):
        print("error: parse recorded anomalies — resolve those first", file=sys.stderr)
        return 2

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        if not con.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (CONVERSATION_ID,)).fetchone():
            print(f"error: conversation {CONVERSATION_ID} not found in {db_path}", file=sys.stderr)
            return 2
        contact_ids = {r[0] for r in con.execute("SELECT id FROM contacts")}
        existing = con.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND provenance = ?",
            (CONVERSATION_ID, PROVENANCE)).fetchone()[0]
        if existing:
            print(f"error: {existing} rows already imported with provenance "
                  f"'{PROVENANCE}' — rollback (DELETE) before re-importing", file=sys.stderr)
            return 2
        min_existing = con.execute(
            "SELECT MIN(created_at) FROM messages WHERE conversation_id = ?",
            (CONVERSATION_ID,)).fetchone()[0]
        last_ts = rows[-1]["ts_utc"]
        if min_existing and last_ts >= min_existing:
            overlapping = con.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND created_at <= ?",
                (CONVERSATION_ID, last_ts)).fetchone()[0]
            print(f"warning: import end {last_ts} >= existing start {min_existing}; "
                  f"{overlapping} existing rows at or before the import's end "
                  f"(expected overlap-gap, not duplicates, but check)", file=sys.stderr)

        missing = sorted({r["sender_id"] for r in rows if r["sender_id"]} - contact_ids)
        if missing:
            print(f"error: mapped contacts missing from DB: {missing}", file=sys.stderr)
            return 2

        # Media first: files are not transactional, so land them before the
        # rows that reference them (and only then insert — a failed copy
        # leaves orphan files, never dangling metadata).
        copied = skipped_existing = 0
        with zipfile.ZipFile(zip_path) as zf:
            for row in rows:
                if not row["media"]:
                    continue
                dest, meta = _dest_for(row["media"], workspace)
                row["metadata"] = {**meta, "wa_message_id": row["media"]["zip_name"]}
                if dry_run:
                    print(f"would copy {row['media']['zip_name']} -> {dest}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    skipped_existing += 1
                else:
                    with zf.open(row["media"]["zip_name"]) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    copied += 1

        if dry_run:
            print(f"dry run: would insert {len(rows)} rows, copy {copied} media files")
            return 0

        con.execute("BEGIN IMMEDIATE")
        try:
            con.executemany(
                """INSERT INTO messages
                   (id, conversation_id, binding_key, role, content, sender_id,
                    channel, metadata, dispatched, synthetic, provenance,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'whatsapp', ?, 1, 0, ?, ?)""",
                [(r["message_id"], CONVERSATION_ID, CONVERSATION_ID, r["role"],
                  r["content"], r["sender_id"],
                  json.dumps(r.get("metadata"), ensure_ascii=False) if r.get("metadata") else None,
                  PROVENANCE, r["ts_utc"]) for r in rows],
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

        n, first, last = con.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM messages "
            "WHERE conversation_id = ? AND provenance = ?",
            (CONVERSATION_ID, PROVENANCE)).fetchone()
        print(f"imported {n} rows ({first} .. {last} UTC); "
              f"media copied={copied} already-present={skipped_existing}")
        print(f"rollback: DELETE FROM messages WHERE provenance = '{PROVENANCE}';")
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="stage", required=True)

    p_parse = sub.add_parser("parse", help="parse zip to staging JSONL + report")
    p_parse.add_argument("zip", type=Path)
    p_parse.add_argument("staging", type=Path)

    p_import = sub.add_parser("import", help="copy media + insert rows")
    p_import.add_argument("staging", type=Path)
    p_import.add_argument("zip", type=Path)
    p_import.add_argument("--db", type=Path,
                          default=Path.home() / "data" / "bob.db")
    p_import.add_argument("--workspace", type=Path,
                          default=Path.home() / "workspace")
    p_import.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if args.stage == "parse":
        return cmd_parse(args.zip, args.staging)
    return cmd_import(args.staging, args.zip, args.db, args.workspace, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
