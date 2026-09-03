"""Assembles prompt messages from workspace files and session history."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from server.services.dispatch_runner import is_no_reply
from server.services.memory.claim_types import ENTITY_TYPES

# Only media rows claimed as THIS turn's stimulus are base64-inlined (the
# newest N of them — a photo-burst claim still shouldn't balloon the prompt).
# Every other media row replays as a text stub naming read_image, so pixels
# are fetched on demand instead of re-billed on every turn; stubs are also
# byte-stable, where an inline window rewrites rows as media ages out and
# busts the prefix cache.
MAX_INLINE_MEDIA = 3

# Dispatch-state markers (2026-08-30 GLM duplication fix). A turn's LLM input
# must make three things visible that plain history replay hides:
# - which user rows are THIS turn's stimulus (claimed from pending) — the
#   model otherwise re-answers messages a prior request already handled;
# - which rows are system notifications, not human speech — goal results,
#   subagent relays etc. masquerade as user messages otherwise;
# - when the replay ends with the prior turn's reply (a message arrived
#   mid-turn and the leftover sweep re-armed), the input must end user-final
#   again — assistant-final inputs make weak models (GLM-5.3-flash, ~11% of
#   turns) regenerate their own last reply verbatim.
NEW_MARKER = "[NEW — awaiting your reply] "
SYSTEM_NOTE_MARKER = "[system notification — not from the human] "
GROUP_EVENT_MARKER = "[Group event] "
STEER_MARKER = "[system relay — steering request] "

# Appended after the replay when the turn's claims are nudges only: these
# turns fold state silently (mirrors _SILENCE_OK_PROVENANCES in
# dispatch_runner) and must not be lured into speaking by their own trailer.
_NUDGE_ONLY_DIRECTIVE = (
    "This turn was triggered by an internal system notification marked "
    "[system notification] above — not by a message from the human. Process "
    "it silently: fold the information into your state or tools. Do not send "
    "a user-visible reply unless a human message above still needs one — if "
    "none does, call {send_tool} with the text NO_REPLY."
)

logger = logging.getLogger(__name__)


def _extract_video_frame(video_path: str) -> str | None:
    """Extract the first frame of a video as a JPEG next to it. Cached on disk.

    Returns the path to the .frame.jpg, or None if ffmpeg is unavailable or
    extraction fails. The cached frame is reused on subsequent calls.
    """
    frame_path = video_path + ".frame.jpg"
    if os.path.isfile(frame_path):
        return frame_path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg not available; cannot extract video frame for %s", video_path)
        return None
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", video_path, "-frames:v", "1", "-q:v", "3", frame_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        logger.exception("failed to extract video frame from %s", video_path)
        return None
    if not os.path.isfile(frame_path) or os.path.getsize(frame_path) == 0:
        return None
    return frame_path

_WORKSPACE_FILES: tuple[str, ...] = ()
_DEPRECATED_WORKSPACE_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")

# Module-level cache for workspace file content.
_cached_prompt: tuple[Any, str] | None = None  # (mtime_hash, content)
_cached_mtime: dict[str, float] = {}


def format_local_now(now: datetime | None = None) -> str:
    """'Tuesday 01 September 2026, 06:52 (AWST, UTC+08:00)' — the shared
    wall-clock stamp for the prompt line and the get_time tool, so the two
    can never drift apart."""
    if now is None:
        now = datetime.now().astimezone()
    tz_name = now.tzname() or "server-local"
    off = now.strftime("%z") or "+0000"
    return (
        f"{now.strftime('%A %d %B %Y, %H:%M')} "
        f"({tz_name}, UTC{off[:3]}:{off[3:]})"
    )


def local_now_prompt_line(*, tools_hint: bool = True) -> str:
    """Turn-scoped clock line (live 2026-09-01: the system prompt carried a
    static 'Timezone: Australia/Perth' but no current time — the model had
    no grounding for date-relative reasoning, and computed UTC cron hours
    into a local-hours routine contract). The stamp is the turn's START:
    turns can run minutes, so precise/mid-turn times must be re-checked.
    tools_hint=False drops the re-check sentence for tool-less calls (goal
    reviser, email-outgoing priming) so the line never advertises tools the
    call doesn't have."""
    line = f"Local time now: {format_local_now()} — taken at this turn's start."
    if tools_hint:
        line += (
            " For precise or mid-turn times, call get_time "
            "(or bash: date '+%A %d %B %Y %H:%M %Z') — and always quote the "
            "timezone it prints when stating times."
        )
    return line


def model_serving_prompt_line(model: str, *, override: bool = False) -> str:
    """Turn-scoped model line. The persona used to claim a static model, but
    the model is dynamic (global default + per-chat /model override), so the
    identity no longer names one and this line states the exact slug serving
    the turn — and whether a chat-level override set it."""
    src = "per-chat /model override" if override else "global default"
    return f"Model serving this turn: {model} ({src})."


async def load_workspace_prompt(workspace_dir: Path, db: Any = None) -> str:
    """Load and concatenate workspace files. Cached until any file changes."""
    global _cached_prompt, _cached_mtime

    workspace_dir = workspace_dir.expanduser()
    mtimes: dict[str, float] = {}
    for name in _WORKSPACE_FILES:
        path = workspace_dir / name
        mtimes[name] = path.stat().st_mtime if path.is_file() else 0.0

    # Include skill file mtimes so new/changed skills invalidate the cache
    skills_dir = workspace_dir / "skills"
    if skills_dir.is_dir():
        for skill_path in sorted(skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue
            md = skill_path / "skill.md"
            if not md.is_file():
                md = skill_path / "SKILL.md"
            if md.is_file():
                mtimes[f"skills/{skill_path.name}"] = md.stat().st_mtime

    # Persona files: healed from the repo bundle at boot, and only rewritten
    # on content change — an mtime move means the persona actually changed.
    from server.services.persona import persona_file_paths
    for rel in persona_file_paths():
        path = workspace_dir / rel
        mtimes[f"self:{rel}"] = path.stat().st_mtime if path.is_file() else 0.0

    mtime_hash = tuple(mtimes.items())
    if _cached_prompt is not None and _cached_prompt[0] == mtime_hash:
        return _cached_prompt[1]

    parts: list[str] = []

    # Persona: rendered from the healed workspace files (self/<name>/*.md +
    # user.md) — the repo bundle is the source of truth, git the history.
    from server.services.persona import get_persona
    rendered_persona = await get_persona(db, workspace_dir=workspace_dir)
    parts.append(rendered_persona)

    for name in _DEPRECATED_WORKSPACE_FILES:
        path = workspace_dir / name
        if path.is_file():
            logger.warning(
                "Deprecated workspace file %s exists — the persona lives in "
                "self/bob/*.md + user.md now",
                name,
            )

    for name in _WORKSPACE_FILES:
        path = workspace_dir / name
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

    # Load skills index (lightweight — full skill loaded on-demand via use_skill tool)
    from server.services.skill_loader import load_skills_index
    skills_index = load_skills_index(workspace_dir)
    if skills_index:
        parts.append("## Available Skills\n\n" + skills_index)

    # Memory tool guidance. The available capture tool depends on the extraction
    # mode (read from env, the same source make_memory_tools uses). A full memory
    # index dump used to be appended here but was disabled — Bob discovers
    # entities on demand via these tools instead.
    if db is not None:
        # Location tool — only when Home Assistant is configured. Mirrors the
        # gating in tool_registry.build_common_tools() so the prompt never
        # advertises a tool that isn't actually registered.
        ha_enabled = (
            os.getenv("BOB_HA_ENABLED", "").lower() in ("true", "1", "yes", "on")
            or (
                bool(os.getenv("BOB_HA_URL", ""))
                and bool(os.getenv("BOB_HA_BEARER_TOKEN", ""))
                and bool(os.getenv("BOB_HA_DEVICE_TRACKER_ENTITY_ID", ""))
            )
        )
        if ha_enabled:
            location_section = (
                "## Location\n\n"
                "- **current_location()** — Return the user's current location "
                "from Home Assistant (zone name, lat/lon, GPS accuracy, "
                "last-updated). Call this BEFORE answering any "
                "location-dependent question: \"where am I\", \"what's near "
                "me\", \"find lunch nearby\", \"how far to X\", \"what should "
                "we do this afternoon\". Cached for 2 min — do not guess "
                "location from chat context.\n"
                "- **location_history(hours=24)** — Return accumulated GPS "
                "pings (every 15 min) from the location_history table. Use "
                "for \"what was my day like\", \"where did we go yesterday\", "
                "\"when did we arrive\", \"how long did we stay\". Lower "
                "`hours` for tighter windows.\n"
            )
        else:
            location_section = ""

        capture_line = (
            "- **remember(hint?)** — Flag the current conversation as worth "
            "capturing; an extraction turn runs right after your reply and "
            "decides what to record. Use sparingly — idle conversations are "
            "mined automatically.\n"
        )
        memory_section = (
            "## Memory\n\n"
            "You have persistent memory with these tools:\n"
            "- **recall(query)** — Look up an entity by ID, name, or natural-language "
            "query; returns its claims rendered as text plus reverse references. "
            "Start here for \"what do I know about X\".\n"
            "- **find(entity_type, claim_type_key?, value?)** — List entities of a "
            "type, optionally filtered by a claim. Useful for listing all dayplans "
            "for a trip, finding a dayplan by date (find(\"dayplan\", \"date\", "
            "\"2026-06-30\")), listing daylogs, finding a daylog by date "
            "(find(\"daylog\", \"date\", \"2026-06-25\")), listing attractions at "
            "a location, etc.\n"
            f"{capture_line}"
            "- **memory_correct(action, entity_id?, claim_type_key?, value?, reason)** "
            "— Fix wrong memory: remove_entity (archive an entity and its claims), "
            "remove_claim (supersede one claim), set_truth (write a user correction). "
            "Always give a reason.\n"
            "\n"
            f"Entity types: {', '.join(ENTITY_TYPES)}.\n"
            "\n"
            "**When to consult memory** — ALWAYS use recall/find before answering "
            "questions about: future plans or schedules (\"what's on tomorrow\", "
            "\"what are we doing Tuesday\"), past events or activities (\"what did "
            "I do yesterday\", \"when did we go to X\"), trip history, people, "
            "places, or anything previously discussed. For future-plan questions, "
            "call find(\"dayplan\", \"date\", \"<YYYY-MM-DD>\") for the resolved "
            "date — and if no dayplan exists, also check the active trip via "
            "recall before claiming there is \"nothing booked\". Do not say "
            "\"I don't know\" or \"I don't have that\" until you have queried "
            "memory. If recall returns nothing relevant, try find() with the "
            "relevant entity type and date/topic filters.\n"
        )
        parts.append(memory_section)
        if location_section:
            parts.append(location_section)

    # Append grounding rules to reduce hallucinated tool claims
    parts.append(
        "## CRITICAL: How to Respond\n"
        "Your text output is NOT delivered to the user. Only tool calls have effect.\n"
        "ALWAYS call send_whatsapp_message (or email_reply) as your final action — even for short replies, "
        "even for acknowledgments, even for jokes. Without that call, nothing is sent.\n"
        "Use as many tools as you need before replying — memory, files, docs, contacts, scripts.\n"
    )
    parts.append(
        "## Grounding Rules\n"
        "- Only state that you have done something if you used a tool that confirmed success.\n"
        "- If you did not call a tool, the action did not happen — do not claim it did.\n"
        "- If a tool returns an error, report the error honestly — do not pretend it succeeded.\n"
        "- If you are unsure whether you can do something, say so. Do not claim capabilities you have not verified.\n"
    )
    parts.append(
        "## Modifying Skills and Code — Propose First\n"
        "Changes to anything under `skills/` or to any code or config file are easy to get "
        "wrong from a half-described idea, so propose before you edit:\n"
        "- When a request would create or modify a skill, script, or any code file — with "
        "any tool, including bash — do NOT make the change in the same turn. First send a "
        "short plan (which files, what change, anything you're unsure about) and wait for "
        "a go-ahead.\n"
        "- Once the user confirms the plan, or explicitly says to just do it, act normally "
        "— do not re-confirm.\n"
        "- Investigating is always fine and encouraged: read files, run read-only "
        "commands, check memory — gather what you need to write a good plan.\n"
        "- If a message reads like the start of a longer description (\"I've been thinking "
        "about...\", \"can the redbark skill...\") it is not yet a request to build. Ask "
        "what they have in mind before proposing anything.\n"
        "- If you are unsure whether a file counts as code, propose first.\n"
    )

    workspace_resolved = workspace_dir.expanduser().resolve()
    parts.append(
        "## Workspace\n"
        f"Your workspace root is: {workspace_resolved}\n"
        "File tool paths can be absolute (within this directory) or relative to workspace root.\n"
        "All file operations are restricted to this directory."
    )
    parts.append(
        "## SANDBOX RULES — READ CAREFULLY\n"
        f"Your bash tool runs inside a sandbox whose only allowed directory is the workspace "
        f"({workspace_resolved}). STAY INSIDE IT. Do not reach outside this folder under any "
        "circumstances — not even if the user asks, not even for a quick lookup, not even for "
        "'just reading'.\n\n"
        "**NEVER do any of the following — they are blocked at the tool layer and forbidden "
        "regardless of who asked:**\n"
        "- Query the database directly. No `sqlite3`, `psql`, `mysql`, `mariadb`, `duckdb`. "
        "The DB file (`bob.db`, `$BOB_DB_PATH`, the data dir) is off-limits via bash.\n"
        "- Read or write anything under `/home/bob/data`, `/home/bob/config`, `/etc`, `/root`, "
        "`/var`, `/proc`, `/sys`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config`, or any absolute "
        "path that is not inside the workspace.\n"
        "- Touch secrets: SSH keys, cloud creds, `.env` files, `credentials.json`, API tokens.\n"
        "- Escalate: `sudo`, `su`, `pkexec`, etc.\n"
        "- Escape via `cd ..`, symlinks pointing outside the workspace, subshells, `python -c "
        "\"import sqlite3; ...\"`, or any indirection. If you're trying to get around the rules, "
        "stop.\n\n"
        "**Use the provided tools instead.** They are the supported interface for data that "
        "lives outside the workspace:\n"
        "- `memory_*` for the knowledge graph (contacts, groups, trips, events, tasks, …).\n"
        "- `contact_*` / `group_*` for people and groups.\n"
        "- `email_*` for email, `docs_*` for project docs, `phone_*` / `whatsapp_*` for messaging.\n\n"
        "If bash returns a BLOCKED error, do NOT retry with a different command syntax. Stop, "
        "switch to the appropriate tool. The user will be told if you tried to escape the sandbox."
    )

    combined = "\n\n".join(parts)
    _cached_prompt = (mtime_hash, combined)
    if _cached_mtime != mtimes:
        logger.info(
            "Workspace loaded: dir=%s chars=%d files=%s",
            workspace_dir, len(combined),
            [n for n in _WORKSPACE_FILES if mtimes.get(n)],
        )
        _cached_mtime.update(mtimes)
    return combined


def _resolve_mentions(text: str, mention_names: dict[str, str]) -> str:
    """Replace @digits patterns with display names from the mention map."""
    def _replace(m: re.Match[str]) -> str:
        digits = m.group(1)
        name = mention_names.get(digits)
        return f"@{name}" if name else m.group(0)
    return re.sub(r"@(\d{7,15})", _replace, text)


async def build_chat_messages(
    user_message: str | list[dict[str, Any]] | None = None,
    session_key: str = "",
    *,
    db: Any = None,
    system_content: str = "",
    voice_instructions: str = "",
    max_history: int = 20,
    claimed_ids: set[str] | frozenset[str] | None = None,
    send_tool_name: str = "",
    include_time: bool = True,
    current_model: str | None = None,
    current_model_override: bool = False,
) -> list[dict[str, Any]]:
    """Build a messages array: system prompt + session history + optional user message.

    ``claimed_ids`` are the message ids this dispatch turn claimed from pending
    (DispatchRunner). When set, claimed human/group-event rows are marked
    [NEW — awaiting your reply], system-generated rows (wake_nudge, group_event,
    steer provenances) are labelled so they never read as human speech, and — when the
    replay ends with the prior turn's reply (mid-turn-arrival shape) — a trailing
    user turn re-presents the new stimulus so the input ends user-final. Turns
    claimed by nudges alone get a system directive to fold silently instead.
    ``send_tool_name`` names the turn's delivery tool in those trailers.
    ``include_time`` appends the turn-start local clock to the system message
    (2026-09-01 fan-out: every channel's turns ground date-relative reasoning).
    ``current_model`` appends the turn's serving model beside the clock — the
    persona stopped naming a model when switching went live; only callers that
    actually resolve a per-turn model pass it (dispatch, routines).
    A system message is therefore emitted even when system_content and
    voice_instructions are both empty; include_time=False restores the
    pre-clock shape exactly.
    """
    system_parts: list[str] = []
    if system_content:
        system_parts.append(system_content)
    if voice_instructions:
        system_parts.append(voice_instructions)
    # Appended LAST, not prepended: provider prompt caching is prefix-based,
    # so a volatile clock at the front would forfeit the stable persona
    # prefix every turn. The "Local time now:" substring guard keeps the
    # injection idempotent for callers that already carry the line.
    if include_time and not any("Local time now:" in p for p in system_parts):
        system_parts.append(local_now_prompt_line())
    # Same appended-last slot, same idempotency guard pattern: the model can
    # change turn-to-turn (/model), so it must never sit in the stable prefix.
    if current_model and not any("Model serving this turn:" in p for p in system_parts):
        system_parts.append(model_serving_prompt_line(
            current_model, override=current_model_override))

    messages: list[dict[str, Any]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    # Dispatch-state bookkeeping for this turn's trailers (claimed_ids set
    # means a dispatch turn): the display lines of claimed human/group-event
    # rows, and whether any claim was a system nudge.
    lifted: list[str] = []
    nudge_claimed = False
    group_event_claimed = False

    if session_key and db is not None:
        is_group = ":group:" in session_key

        # For group sessions, resolve sender_id to display names
        sender_names: dict[str, str] = {}
        mention_names: dict[str, str] = {}
        if is_group:
            from server.repositories.participants import ParticipantRepository
            participants = await ParticipantRepository(db).list_for(session_key)
            for p in participants:
                if p["contact_id"] and p["display_name"]:
                    sender_names[p["contact_id"]] = p["display_name"]
                if p["display_name"] and p["identifier"]:
                    digits = re.sub(r"\D", "", p["identifier"])
                    if digits:
                        mention_names[digits] = p["display_name"]

        from server.repositories.history import HistoryRepository
        rows = await HistoryRepository(db).recent_dialogue(
            session_key, limit=max_history)

        # Indices of the last N assistant rows — these get full tool-block
        # replay. Older rows fall back to the short summary prefix.
        last_assistant_indices: set[int] = set()
        seen = 0
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["role"] == "assistant":
                last_assistant_indices.add(i)
                seen += 1
                if seen >= 3:
                    break

        # Indices of the media rows claimed as this turn's stimulus — the
        # only rows that get base64-inlined images/frames, capped at the N
        # newest. All other media rows (and all media on claim-less replays,
        # e.g. subagents and wake turns) replay as text stubs naming
        # read_image, so the model fetches pixels on demand instead of every
        # past photo riding every turn.
        inline_media_indices: set[int] = set()
        if claimed_ids is not None:
            media_seen = 0
            for i in range(len(rows) - 1, -1, -1):
                if rows[i]["role"] != "user" or rows[i].get("id") not in claimed_ids:
                    continue
                raw = rows[i].get("metadata")
                if not raw:
                    continue
                try:
                    m = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    continue
                if m.get("image_path") or m.get("video_path"):
                    inline_media_indices.add(i)
                    media_seen += 1
                    if media_seen >= MAX_INLINE_MEDIA:
                        break

        for i, row in enumerate(rows):
            if not row["content"]:
                continue
            # Skip stale NO_REPLY entries that poison future decisions
            if row["role"] == "assistant" and is_no_reply(row["content"]):
                continue
            content = row["content"]
            if is_group and mention_names:
                content = _resolve_mentions(content, mention_names)

            # Dispatch-state markers, dispatch turns only (claimed_ids set —
            # non-dispatch callers keep the byte-for-byte old replay shape).
            # Claimed non-nudge rows are this turn's stimulus; system-generated
            # rows are labelled whether or not they are the stimulus, so
            # historical nudges never read as human speech. NEW is prepended
            # last so it reads outermost: "[NEW] [Group event] …".
            if claimed_ids is not None:
                prov = row.get("provenance") or ""
                is_claimed = (row["role"] == "user"
                              and row.get("id") in claimed_ids)
                if prov == "wake_nudge":
                    content = SYSTEM_NOTE_MARKER + content
                    if is_claimed:
                        nudge_claimed = True
                elif prov == "group_event":
                    content = GROUP_EVENT_MARKER + content
                    if is_claimed:
                        group_event_claimed = True
                elif prov == "steer":
                    # Steering requests (services/steering.py): attributed in
                    # their own content header, marked here as a system relay
                    # so they never read as someone speaking in this chat.
                    # Claimed rows fall through to the NEW marker below — a
                    # steer IS this turn's stimulus, not a fold-silently nudge.
                    content = STEER_MARKER + content
                if is_claimed and prov != "wake_nudge":
                    content = NEW_MARKER + content
                    sender_prefix = ""
                    if is_group and row["sender_id"]:
                        name = sender_names.get(row["sender_id"])
                        if name:
                            sender_prefix = f"[{name}] "
                    lifted.append(f"{sender_prefix}{content}")

            # Check for image metadata and reconstruct multimodal content
            meta: dict[str, Any] = {}
            raw_meta = row.get("metadata")
            if raw_meta:
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                except (json.JSONDecodeError, TypeError):
                    pass
            image_path = meta.get("image_path")
            mime_type = meta.get("image_mime_type", "image/jpeg")
            video_path = meta.get("video_path")
            is_gif = bool(meta.get("is_gif"))

            if video_path and row["role"] == "user" and os.path.isfile(video_path):
                text_prefix = ""
                if is_group and row["sender_id"]:
                    name = sender_names.get(row["sender_id"])
                    if name:
                        text_prefix = f"[{name}] "
                attachment_note = "[GIF attached]" if is_gif else "[Video attached]"
                text_content = text_prefix + (content if content and content not in ("[GIF]", "[Video]") else attachment_note)
                if i not in inline_media_indices:
                    messages.append({"role": "user", "content": f"{text_content} (video file at {video_path} — view its first frame with read_image)"})
                    continue
                frame_path = _extract_video_frame(video_path)
                if frame_path and os.path.isfile(frame_path):
                    with open(frame_path, "rb") as f:
                        frame_data = base64.b64encode(f.read()).decode()
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": f"{text_content} (first frame shown; file at {video_path})"},
                            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{frame_data}"},
                        ],
                    })
                else:
                    messages.append({"role": "user", "content": f"{text_content} (video file at {video_path} — view its first frame with read_image)"})
                continue

            if image_path and row["role"] == "user" and os.path.isfile(image_path):
                text_prefix = ""
                if is_group and row["sender_id"]:
                    name = sender_names.get(row["sender_id"])
                    if name:
                        text_prefix = f"[{name}] "
                if i not in inline_media_indices:
                    messages.append({
                        "role": "user",
                        "content": f"{text_prefix}{content} (image file at {image_path} — view it with read_image)",
                    })
                    continue
                with open(image_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode()
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"{text_prefix}{content} (file at {image_path})"},
                        {"type": "input_image", "image_url": f"data:{mime_type};base64,{image_data}"},
                    ],
                })
                continue

            if is_group and row["role"] == "user" and row["sender_id"]:
                name = sender_names.get(row["sender_id"])
                if name:
                    messages.append({"role": "user", "content": f"[{name}] {content}"})
                    continue

            if row["role"] == "assistant":
                # Tool trace replay. Last 3 assistant rows expand full
                # function_call / function_call_output items inline; older
                # rows with a summary get a bracketed prefix.
                expanded = False
                if i in last_assistant_indices and row.get("tool_blocks_json"):
                    try:
                        items = json.loads(row["tool_blocks_json"])
                    except (json.JSONDecodeError, TypeError):
                        items = None
                    if isinstance(items, list) and items:
                        for item in items:
                            if isinstance(item, dict):
                                messages.append(item)
                        if content:
                            messages.append({"role": "assistant", "content": content})
                        expanded = True
                if not expanded:
                    summary = row.get("tool_summary")
                    if summary:
                        content = f"{summary}\n\n{content}" if content else summary
                    messages.append({"role": "assistant", "content": content})
                continue

            messages.append({"role": row["role"], "content": content})

    if user_message is not None:
        messages.append({"role": "user", "content": user_message})

    # -- dispatch trailers ------------------------------------------------
    # Only for dispatch turns (claimed_ids set). See module docstring: the
    # model must never face an input whose last item is its own prior reply
    # with the new stimulus buried unmarked mid-replay.
    if claimed_ids is not None:
        no_reply_ref = (
            f"call {send_tool_name} with the text NO_REPLY"
            if send_tool_name else "reply NO_REPLY via your send tool"
        )
        if lifted and _ends_with_assistant_side(messages):
            lines = "\n".join(f"- {t}" for t in lifted)
            group_event_hint = (
                "\nGroup events don't always need a reply — greet a new member "
                "or acknowledge a notable departure when it fits; otherwise "
                f"{no_reply_ref} is fine."
                if group_event_claimed else ""
            )
            messages.append({
                "role": "user",
                "content": (
                    "New message(s) received since your last reply (also marked "
                    f"[NEW] above):\n{lines}\n\n"
                    "Your earlier replies in this conversation were already "
                    "delivered. Respond only to the new message(s) above and do "
                    "not repeat or restate anything you have already sent. Rows "
                    "marked [system notification] are internal bookkeeping, not "
                    f"messages from the human. If the new message(s) are already "
                    f"fully answered by your last reply, {no_reply_ref}."
                    f"{group_event_hint}"
                ),
            })
        elif nudge_claimed and not lifted:
            messages.append({
                "role": "system",
                "content": _NUDGE_ONLY_DIRECTIVE.format(
                    send_tool=send_tool_name or "your send tool"),
            })
    return messages


def _ends_with_assistant_side(messages: list[dict[str, Any]]) -> bool:
    """True when the last item belongs to an assistant turn — an assistant
    text message or replayed tool-trace items (function_call /
    function_call_output / reasoning). Used to decide whether a trailing
    user turn must re-present the current turn's stimulus."""
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") == "assistant":
        return True
    return last.get("type") in ("function_call", "function_call_output", "reasoning")
