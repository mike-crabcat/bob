"""One-off self-bob claim cleanup (2026-09-14 self-memory review).

122 active claims; review found ~54% noise: ~30 dated incident stories,
~12 tool how-tos, build logs, duplicate families, 3 stale-now-false limits,
2 future-dated ("Nov 2026") rows. This script distils that pile into the
new typed vocabulary (self_state / practice / feedback / incident —
migration 006) and retires the rest:

  - keeps durable limits/capabilities as-is
  - converts should-rules into `practice` claims (one distilled rule each;
    the incident narratives they came from are retracted)
  - converts human assessments into `feedback` (fixing the future dates)
  - writes current-state facts as `self_state`
  - retracts incidents/build logs/dupes/stale rows with traceable reasons
  - relocates tool how-tos into the relevant workspace skill files

Safety: dry-run by default; `--apply` writes. Before applying, every
self-bob row is dumped to a rollback JSON. Any active self-bob claim NOT
covered by the classification aborts the run — nothing is silently
reclassified. Requires migration 006 (new claim types) to be applied.

Usage:
  uv run python scripts/cleanup_self_bob_claims.py            # dry-run
  uv run python scripts/cleanup_self_bob_claims.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server.database import Database  # noqa: E402

SELF_ID = "self-bob"
CLEANUP_TAG = "self-bob-cleanup-2026-09-14"

# ── Classification ──────────────────────────────────────────────────────

# Durable "can't" constraints — stay as limit.
KEEP = {
    "claim-extr-2039259f", "claim-recon-852c42b4", "claim-extr-13023e72",
    "claim-extr-314c1715", "claim-extr-dd1bfbdd", "claim-extr-50468991",
    "claim-extr-ea980d1f", "claim-extr-4f459fc3", "claim-extr-25caa87f",
    "claim-extr-08f7be83", "claim-recon-67e5b22c", "claim-extr-2b455575",
    "claim-recon-a448fe04", "claim-seed-limit-family-scope",
    "claim-extr-efb189f0", "claim-extr-9ce9edc5", "claim-extr-ef52a6e7",
    "claim-extr-006d0e48", "claim-extr-3640f9fb",
    # capability keeps
    "claim-extr-68bd4951", "claim-extr-265c498b", "claim-extr-59397dbb",
    "claim-extr-c5b00c1b", "claim-extr-512f6486", "claim-extr-ee79a35f",
    "claim-extr-5367336d", "claim-extr-8e1304af", "claim-recon-31fa08c7",
    "claim-recon-b7a87e99",
    # milestone / value / self_image / assigned_identity: all kept as-is.
}

# Distilled standing rules — the episode narratives they replace are retracted.
PRACTICES: list[tuple[str, list[str]]] = [
    ("Verify the recipient and channel before cross-channel sends — group-send vs DM misroutes have leaked private details.",
     ["claim-extr-fae09bdc", "claim-extr-daed371a"]),
    ("Keep conversations strictly separated — never carry facts or inferences from one chat into another without an explicit shared basis (recurring cross-thread bleed).",
     ["claim-extr-bef23623", "claim-extr-d144ddb2", "claim-extr-efc2cbb9"]),
    ("Query the actual source system before answering schedule/state questions — chat context goes stale.",
     ["claim-extr-54960453"]),
    ("Never announce an action as completed before it has actually executed (bookings, sends, orders).",
     ["claim-extr-52417d8a"]),
    ("Check for existing outputs and state before regenerating or rebuilding — duplicate work burns trust.",
     ["claim-extr-df4b7215", "claim-extr-acbca242"]),
    ("Never guess file paths, config values, or API keys — list, search, or read the source.",
     ["claim-extr-5c72d6ef", "claim-extr-95aaa0e9"]),
    ("Look for bulk/batch endpoints before building per-item loops.",
     ["claim-extr-74f15824"]),
    ("Check faces in generated portrait/promo images before sending them.",
     ["claim-extr-1d31f6b8"]),
    ("Render how artwork looks ON the product before fulfilling a print order — format/DPI validation is not enough.",
     ["claim-extr-5b6fbc02"]),
    ("Use one face reference per image edit — multiple refs can merge two people's features.",
     ["claim-extr-f9000b91"]),
    ("Don't AI-upscale finished artwork (it reinterprets it) — re-render at target size.",
     ["claim-extr-39bd1568"]),
    ("Embargoed feature builds never touch the live library before airtime — quarantine means quarantine.",
     ["claim-extr-e6efe1ac"]),
    ("Check stimulus feeds for sibling/duplicate events before reporting, and never infer identity without direct evidence.",
     ["claim-extr-e25f1915"]),
    ("'Works from here' is not independent evidence — verify user-reported access from outside the serving host.",
     ["claim-extr-b7dafae8"]),
    ("Background script jobs take the bare command, not objective prose.",
     ["claim-extr-7598bd38"]),
    ("Injected operational/system blocks and raw tool logs are private metadata — never summarise, confirm, relay, or post them into chats.",
     ["claim-extr-5b359a74", "claim-extr-f6ee93a6"]),
    ("Throwaway file servers must never squat shared routes (tailscale serve on '/').",
     ["claim-extr-caf2daf2"]),
    ("Watch for images bleeding across message contexts — an image-context bug can carry one message's images into later inputs.",
     ["claim-extr-1515375f"]),
    ("Complete the requested phone-call agenda including required voicemails; unsolicited robot chatter annoys group members.",
     ["claim-extr-b1d07bd0"]),
]

# Attributed human assessments (future dates fixed).
FEEDBACK: list[tuple[str, list[str]]] = [
    ("Brad (2026-09-11, phone feedback): latency/responsiveness 'pretty good' for a live phone agent; voice accent reads as British — would prefer Californian.",
     ["claim-extr-23f59369"]),
    ("Mike (2026-09-11): has only fired natural on-his-own gif reactions twice so far — wants more spontaneous reactions.",
     ["claim-extr-ac389f0a"]),
]

# Current-state facts.
SELF_STATE: list[tuple[str, list[str]]] = [
    ("primary model: glm-5.3-flash (per Mike, early Sept 2026; /model aliases cheap/asi/chinese exist)",
     ["claim-extr-7a6b4ade"]),
    ("wake model: messages plus scheduled wakeups/routines/goal dues — idle background processing exists (since late Aug 2026)",
     ["claim-extr-74ca3881"]),
]

# Plain retractions: id -> reason.
RETRACT: dict[str, str] = {
    # stale / no longer true
    "claim-extr-c015bf63": "stale: quote-reply context is visible since 2026-09-13",
    "claim-extr-91b45b47": "stale: Perth Shipping News segment is live",
    # incident narratives whose lesson lives in a practice claim or milestone
    "claim-extr-191fb4c2": "incident narrative (persona-override momentum)",
    "claim-extr-64d6cc2e": "incident narrative (promo artwork mix-up)",
    "claim-extr-90959e60": "incident narrative (approval-governance episode)",
    "claim-extr-63620572": "historical record (rescinded rules; current rule lives in skill.md)",
    "claim-extr-d1d8ec05": "incident narrative (likeness failures)",
    "claim-extr-6e0cc32f": "incident narrative (fixed: SCHEDULED line, 2026-09-13)",
    "claim-extr-1c468cb6": "incident narrative (clock-check rule lives in practice)",
    "claim-extr-21ccdea6": "incident narrative (sibling/identity rule lives in practice)",
    "claim-extr-161dbee9": "incident narrative (milestone claim holds the record)",
    "claim-extr-f724953f": "incident narrative (fixed: delivery-truth template 2026-09-05)",
    "claim-extr-5f30ac37": "incident narrative (milestone claim holds the record)",
    # build logs / ops journals — git/CHANGELOG material
    "claim-extr-85594718": "build log (git history material)",
    "claim-extr-c1c014a3": "ops log (FTP sweep journal)",
    "claim-extr-fab6db39": "build log (git history material)",
    "claim-extr-c3963183": "build log / progress journal",
    "claim-extr-218d069e": "stale state snapshot (draft pack)",
    "claim-extr-b45262f3": "build log (git history material)",
    "claim-extr-0e2d3050": "build log (git history material)",
    "claim-extr-dec115ac": "build log (delivery confirmed = changelog)",
    "claim-extr-0759fa94": "build log (git history material)",
    # tool how-tos — relocated to workspace skill files
    "claim-extr-9a57cf87": "tool how-to → skills/radio/skill.md",
    "claim-extr-3d27af28": "tool how-to → skills/radio/skill.md",
    "claim-extr-be9f2de8": "tool how-to → skills/videogen/skill.md",
    "claim-extr-46c0065e": "tool how-to → skills/videogen/skill.md",
    "claim-extr-dd287a47": "tool how-to → skills/videogen/skill.md",
    "claim-extr-eb9c25a0": "tool how-to → skills/videogen/skill.md",
    "claim-extr-8840026f": "tool how-to → skills/videogen/skill.md",
    "claim-extr-1b4c37c8": "tool how-to → skills/openai-image/skill.md",
    "claim-extr-d97f1bc8": "tool how-to → skills/openai-image/skill.md",
    "claim-extr-21362ec7": "tool how-to → skills/printful/skill.md",
    "claim-extr-20ec2d0f": "tool how-to → skills/printful/skill.md",
    "claim-extr-478e1dc8": "tool how-to → skills/printful/skill.md",
    "claim-recon-eea4a8d5": "tool how-to → skills/printful/skill.md",
    "claim-extr-3aeef5b5": "tool how-to → skills/printful/skill.md",
    # duplicates / misplaced
    "claim-extr-c111c516": "duplicate of claim-recon-b7a87e99 (Shipping News commission)",
    "claim-extr-03f7a2e1": "misplaced (music-library reference, not a capability)",
}

# Tool how-tos relocated into workspace skill files.
SKILL_NOTES: dict[str, list[str]] = {
    "radio": [
        "radio.py fetch exits 0 but silently skips tracks already in the library without queueing them — verify the queue after fetch, don't trust exit 0.",
        "Spotify rate-limits (429) can persist for hours — fall back to the NAS collection / other sourcing instead of retrying.",
    ],
    "videogen": [
        "Gemini Omni Flash 1.1 is the default video model (solves facial expressions, ~43s per 5s clip); Gemini models are weak at replicating a fixed character design — prefer Wan when character fidelity matters.",
        "Wan 3.0 rejects square sizes like 1024x1024 (HTTP 400 unsupportedModelResolution) — use 1280x720 or 720x1280.",
        "Read the Runware key from config — never scrape/regex it out of source (produced a 401).",
        "Runware imageUpscale works with the existing key (imageUpload + 2x upscale).",
        "Post reaction clips to WhatsApp as silent mp4 (WhatsApp auto-loops them); gifs don't loop properly.",
    ],
    "openai-image": [
        "Prompts containing apostrophes must be written to a file and passed via --prompt-file — inline single-quoted prompts die on shell quoting.",
        "Image-edits that fully cover a child's face in fur/animal features can trip output moderation — rephrase the prompt to be less transformative.",
    ],
    "printful": [
        "Decimating hollow-shell meshes pokes holes at any face count — voxel remesh at full density is the reliable watertight path; modern slicer auto-repair handles a few non-manifold edges fine.",
        "Hunyuan mesh repair workflow for printable figurines: pymeshlab weld/stitch pass over the raw glb beats re-voxelling for surface detail.",
        "Runware 3D: image-to-3D via tencent:hunyuan-3d@3.1-pro (Tripo v3.1 also available); the 3D API rejects ALL legacy settings (imageAug etc.) — inputs payload only, no text guidance.",
    ],
}


def _converted_ids() -> dict[str, str]:
    out: dict[str, str] = {}
    for new_id, _, sources in [
        (f"claim-cleanup-p{i:02d}", rule, srcs) for i, (rule, srcs) in enumerate(PRACTICES, 1)
    ] + [
        (f"claim-cleanup-f{i:02d}", val, srcs) for i, (val, srcs) in enumerate(FEEDBACK, 1)
    ] + [
        (f"claim-cleanup-s{i:02d}", val, srcs) for i, (val, srcs) in enumerate(SELF_STATE, 1)
    ]:
        for s in sources:
            out[s] = new_id
    return out


async def main(apply: bool) -> None:
    db_path = Path(os.getenv("BOB_DB_PATH", str(Path.home() / "data" / "bob.db")))
    db = Database(db_path=db_path, schema_dir=REPO / "server" / "schemas", pool_size=1)
    await db.connect()
    # Migration 006 (new claim types) is tracked + idempotent; applying it
    # here lets the cleanup run before a server restart ships the new code
    # (the running server simply ignores claim types it doesn't know).
    await db.apply_migrations()

    try:
        await _run(db, apply, db_path=db_path)
    finally:
        await db.close()


async def _run(db, apply: bool, *, db_path: Path) -> None:
    from server.services.memory.claim_service import write_claim, update_entity_fts
    from server.services.memory.models import Claim

    rows = await db.fetch_all(
        "SELECT id, claim_type_key, COALESCE(value, object_id) AS v, created_at "
        "FROM memory_claims WHERE subject_id = ? AND status = 'active'",
        (SELF_ID,))
    by_id = {r["id"]: r for r in rows}

    # Migration 006 guard: conversions need the new types to exist.
    have_types = {
        r["key"] for r in await db.fetch_all(
            "SELECT key FROM memory_claim_types "
            "WHERE key IN ('self_state', 'practice', 'feedback', 'incident')")}
    missing = {"self_state", "practice", "feedback"} - have_types
    if missing:
        print(f"ABORT: claim types missing (apply migration 006 first): {missing}")
        return

    converted = _converted_ids()
    classified = KEEP | set(converted) | set(RETRACT)

    # milestone / value / self_image / assigned_identity are kept wholesale —
    # only the landfill types (limit/capability/truth) must be classified.
    implicitly_kept = {"milestone", "value", "self_image", "assigned_identity"}
    unclassified = [i for i, r in by_id.items()
                    if i not in classified and r["claim_type_key"] not in implicitly_kept]
    if unclassified:
        print("ABORT: active self-bob claims not covered by the classification:")
        for i in unclassified:
            print(f"  {i} [{by_id[i]['claim_type_key']}] {by_id[i]['v'][:90]}")
        return
    gone = [i for i in classified if i not in by_id]
    if gone:
        print(f"ABORT: classified ids no longer active/present: {gone}")
        return

    n_keep = sum(1 for i in KEEP)
    n_conv = len(converted)
    n_ret = len(RETRACT)
    print(f"self-bob active claims: {len(rows)}")
    print(f"  keep:                {n_keep}")
    print(f"  convert (retype):    {n_conv}  -> {len(PRACTICES)} practice, "
          f"{len(FEEDBACK)} feedback, {len(SELF_STATE)} self_state")
    print(f"  retract:             {n_ret}  "
          f"({sum(1 for r in RETRACT.values() if 'incident' in r)} incident, "
          f"{sum(1 for r in RETRACT.values() if 'build log' in r or 'ops log' in r or 'snapshot' in r)} buildlog/stale-state, "
          f"{sum(1 for r in RETRACT.values() if 'tool how-to' in r)} tool how-to, "
          f"{sum(1 for r in RETRACT.values() if 'stale:' in r)} stale, "
          f"{sum(1 for r in RETRACT.values() if 'duplicate' in r or 'misplaced' in r)} dupe/misplaced)")
    print(f"skill notes: {sum(len(v) for v in SKILL_NOTES.values())} bullets across "
          f"{len(SKILL_NOTES)} skills")

    if not apply:
        print("\nDRY RUN — no changes made. Re-run with --apply.")
        return

    # Rollback dump: every self-bob row, full columns, before any mutation.
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    all_rows = await db.fetch_all(
        "SELECT * FROM memory_claims WHERE subject_id = ?", (SELF_ID,))
    backup_path = backup_dir / f"{CLEANUP_TAG}.json"
    backup_path.write_text(json.dumps(
        {"tag": CLEANUP_TAG, "dumped_at": datetime.now().isoformat(),
         "rows": [dict(r) for r in all_rows]}, indent=1, default=str))
    print(f"\nrollback dump: {backup_path} ({len(all_rows)} rows)")

    now = datetime.now()

    async def write_new(claim_id: str, type_key: str, value: str) -> None:
        await write_claim(db, Claim(
            id=claim_id, claim_type_key=type_key, subject_id=SELF_ID,
            value=value, status="active", created_at=now))

    async def retract(claim_id: str, supersede_ref: str) -> None:
        await db.execute(
            "UPDATE memory_claims SET status = 'retracted', superseded_by = ? "
            "WHERE id = ?",
            (json.dumps([supersede_ref]), claim_id))

    for i, (rule, _) in enumerate(PRACTICES, 1):
        await write_new(f"claim-cleanup-p{i:02d}", "practice", rule)
    for i, (value, _) in enumerate(FEEDBACK, 1):
        await write_new(f"claim-cleanup-f{i:02d}", "feedback", value)
    for i, (value, _) in enumerate(SELF_STATE, 1):
        await write_new(f"claim-cleanup-s{i:02d}", "self_state", value)

    for src, new_id in converted.items():
        await retract(src, new_id)
    for claim_id, reason in RETRACT.items():
        await retract(claim_id, f"{CLEANUP_TAG}: {reason}")

    # Relocate tool how-tos into skill files.
    workspace = Path(os.getenv("BOB_HARNESS_WORKSPACE_DIR",
                               str(Path.home() / "workspace")))
    for skill, notes in SKILL_NOTES.items():
        md = workspace / "skills" / skill / "skill.md"
        if not md.is_file():
            md = workspace / "skills" / skill / "SKILL.md"
        if not md.is_file():
            print(f"  WARNING: skill file missing, note skipped: {md}")
            continue
        existing = md.read_text(encoding="utf-8")
        if "Gotchas (from memory" in existing:
            continue
        block = ("\n\n## Gotchas (from memory, 2026-09-14)\n\n"
                 + "\n".join(f"- {n}" for n in notes) + "\n")
        md.write_text(existing.rstrip("\n") + block, encoding="utf-8")
        print(f"  skill notes appended: {skill}/skill.md (+{len(notes)})")

    await update_entity_fts(db, SELF_ID)

    after = await db.fetch_all(
        "SELECT claim_type_key, COUNT(*) n FROM memory_claims "
        "WHERE subject_id = ? AND status = 'active' GROUP BY claim_type_key "
        "ORDER BY n DESC", (SELF_ID,))
    total = sum(r["n"] for r in after)
    print(f"\nDONE. self-bob active claims now: {total}")
    for r in after:
        print(f"  {r['claim_type_key']}: {r['n']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually mutate (default: dry-run)")
    asyncio.run(main(ap.parse_args().apply))
