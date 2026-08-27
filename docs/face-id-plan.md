# Face ID — identify people in photos Bob receives

Status: **phase 1 implemented** (skill + galleries + validation, 2026-08-26) · Written 2026-08-26
Motivation: people send Bob photos for imagegen; sender identity and photo
content are currently unlinked. Catching "Sean shares Mike's photo as a photo
of himself" needs face identity as first-class message metadata.

## Goal

When a photo arrives on any channel, Bob knows whose face(s) are in it —
as automatic metadata the dispatch LLM sees alongside sender attribution,
and as an on-demand skill for ad-hoc queries ("who's in this?"). All
recognition runs locally on the workstation; no photos or embeddings leave
the box.

## Non-goals

- **Auto-accusation / moderation.** Mismatches are surfaced as annotations;
  Bob's persona decides tone. No blocking, warning DMs, or auto-replies.
- **Video/GIF frame identification.** Inbound still images only (v1).
- **Enrollment from untrusted third-party claims.** "Here's a photo of Sean"
  from an untrusted sender never enrolls — that is the attack we detect.
- **Face generation/training.** No fine-tuning, no new model training;
  off-the-shelf detection + embeddings.

## Settled design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **InsightFace `buffalo_l`** (RetinaFace detection + ArcFace 512-d embeddings, onnxruntime CPU) | Deterministic, repeatable, ~200 ms/photo, free, fully local. A VLM "same person?" call is neither repeatable nor cheap enough to run on every inbound photo. |
| D2 | **Gallery lives in the workspace**: `workspace/people/<slug>/faces.json` beside the photos | Bob's bash is sandboxed to `~/workspace` (API-security remediation), so a skill CLI cannot read `~/data/bob.db` — the workspace is the only store both the server-side hook and the skill can touch. Also human-inspectable and versioned with the existing `people/` convention. |
| D3 | **Attested enrollment** (reviewed 2026-08-26): a face enters person X's gallery from (a) a photo **X sent of themselves** (any trust level — DM selfie or "this is me"), or (b) a photo a **trusted sender** attests is X (Mike or any `is_trusted` contact sharing a photo of someone else). Attester recorded in provenance. | Self-attestation is the anti-poisoning core; trusted attestation solves cold-start for people who never DM Bob (Sean). Untrusted third-party attribution is exactly the mislabeling vector we're defending against, and it never enrolls. |
| D4 | **Originals only, never imagegen outputs** | Edited/generated faces drift from the source embedding; enrolling them creates phantom matches and mismatches. |
| D5 | **Margin rule, not bare threshold**: identify only when top-1 cosine beats top-2 decisively (initial: top-1 ≥ 0.35 **and** margin ≥ 0.08) | "No guess" beats "wrong guess" between lookalike mates; weak/angled/small faces fall through to unknown. |
| D6 | **Model id pinned in the gallery** (`model: buffalo_l@<version>` in every `faces.json`) | ArcFace embeddings are not comparable across model versions; a model bump means re-embedding all references, and the pin makes that detectable instead of silent. |
| D7 | **Annotate at ingest, don't prompt-engineer** | Faces ride the event payload / `messages.metadata` like `sender_name` does; the dispatch LLM sees "photo sent by Sean contains mike-cleaver" with zero prompt changes. |

## Architecture

Two layers over one engine:

1. **Skill (explicit)** — `workspace/skills/faces/`: `skill.md` + CLI
   (`identify <path>`, `enroll`, `gallery`, `whois`). Same pattern as
   merch/redbark/printful. Weights under `workspace/skills/faces/models/`.
2. **Ingest hook (automatic)** — in the WhatsApp bridge, immediately after
   `image_path` resolves (`_service.py` ~L478–496): detect → embed → match →
   write into the event payload (beside `has_media`) and `messages.metadata`:

   ```json
   "faces": [
     {"match": "mike-cleaver", "sim": 0.43},
     {"match": "sylvain-ayrault", "sim": 0.38},
     {"match": null, "sim": 0.11, "note": "no gallery hit"}
   ]
   ```

   Runs synchronously at ingest (~200 ms; the debounce window absorbs it —
   measure, and only if it exceeds budget move to async-before-watermark).

### Gallery layout

```
workspace/people/<slug>/
  faces.json        # {"model": "...", "refs": [{"file": "...", "embedding": [...], "source": "dm-selfie 2026-08-26"}, ...], "contact_id": "...?"}
  selfie.jpg        # existing reference photos, unchanged
workspace/skills/faces/unknowns.jsonl   # clustered unknown-face embeddings (phase 3)
```

Slug ↔ `contact_id` mapping is optional in `faces.json`; resolution can be
fuzzy (name match via contacts) and filled in lazily.

## Phases

### Phase 1 — skill + validation (no dispatch-path changes)
- Build `skills/faces` CLI; enroll from existing `people/` dirs and
  `photos/mike_cleaver_selfie.png` (Mike ✓; **Sean has no dir yet — seed via
  trusted attestation: one Sean photo from Mike's camera roll**).
- Validation set: existing group photos + the Sean-shares-Mike's-photo
  scenario staged with real photos.
- **Acceptance**: correct ID on clear single-face photos; per-face IDs on a
  4–8 person group photo; no false identification of gallery members on
  distractor photos; small/blurry faces report unknown.

### Phase 2 — ingest annotation (observe-only)
- Bridge hook writes `faces` into event payload + `messages.metadata`.
- No behaviour change beyond context; count annotations and spot-check
  precision on real group traffic for ~a week (careful-rollouts pattern).
- **Acceptance**: annotations present and correct on live photos; ingest
  latency within budget; no dispatch errors.

### Phase 3 — mismatch signal + unknown clustering
- Emit `faces.mismatch` when sender has a gallery and top face match is a
  different person; LLM chooses tone.
- Cluster unknown embeddings (`unknowns.jsonl`); surface "unknown #7 has
  appeared in 12 photos with Sylvain"; Mike can name a cluster → new
  `people/<slug>` gallery.
- Tune D5 thresholds from phase-2 live data.
- **Acceptance**: the staged Sean/Mike case fires the mismatch; zero false
  mismatches on a week of clean traffic.

## Risks & limits

- **Face size**: embeddings need ~80+ px of face; WhatsApp re-encodes to
  ~1600 px, so back rows of large group shots degrade to unknown (correct
  failure mode).
- **Lookalikes** — margin rule mitigates; annotate-first until precision is
  proven on real traffic (D5, phase ordering).
- **Drift over time** (beards, age) — multiple refs per person (3–5) help;
  gallery additions from DMs keep refs current.
- **Privacy** — all-local; no photos or embeddings leave the workstation.
  No disclosure obligations (operator decision 2026-08-26).

## Review decisions (2026-08-26)

- **Trusted contacts may attest photos of others** (folds into D3).
- **No disclosure needed** — Bob does not mention face recognition,
  proactively or otherwise.
- **Defaults accepted unless overridden later:** unknown-face embeddings
  expire after 12 months unnamed; annotate everywhere Bob receives photos
  (all groups + DMs); non-WhatsApp channels defer to phase 3+.
