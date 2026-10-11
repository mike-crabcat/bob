# Prompt layout plan (temporary — fold the outcome into a reference doc, then delete)

Started 2026-10-11 (Mike). Goal: improve Bob's level of function on main
WhatsApp turns through prompt *layout* — measured, staged, reversible — not a
big-bang rewrite of the persona.

## Why

Anatomy of a main WhatsApp turn today (AI Doom, gpt-6-luna, ~67k prompt tokens):

```
[tool schemas]  ~67k chars (~17k tok) — full docstrings, many carry rules
SYSTEM (~54k chars / ~13.5k tok, one message):
  Reply length (injected) · soul.md · identity.md · agents.md · user.md
  Available Skills (14.2k — 26% of system) · Memory/Location
  "CRITICAL: How to Respond" (email-only text, in a WhatsApp turn)
  Grounding · Propose-first · Workspace · Sandbox · Channels
  Self-brief · Participants/Group memory/Roster · Work · Record discipline · MCP
  HARD RULES (terminal contract) + clock + model line
HISTORY ~150 items (~23k tok) … [NEW — awaiting your reply] <stimulus>
TOOL LOOP
```

Findings (2026-10-11 review):
1. The "last instruction" isn't last: HARD RULES end the *system message*, but
   ~23k tokens of history sit between it and generation. The 2026-10-03
   terminal-contract work found *position* was the lever (flash referenced the
   scattered end-of-turn rules in 2% of turns).
2. Guidance is scattered/duplicated (reply length ×3, formatting ×2; grounding,
   record discipline and hard rules are three homes for one principle; tool
   docstrings are a fourth).
3. Mislabelled: "CRITICAL: How to Respond" is email-only.
4. Low-signal bulk: skills catalogue 14.2k chars every turn (avatar canon
   already moved out — c16c4dc1, ~850 tok/turn saved).
5. Per-turn volatile blocks sit before the history (cache-stable prefix
   can't include the history). Measured caching is mostly fine today.
6. No compact "situation" statement for the turn.

Not the cause of most 2026-10-09/10 incidents (those were plumbing — dropped
final texts, image fold loop, routine/broadcast gaps — or judgement), so the
expected gain is real but unproven: measure everything.

## Phases

### Phase 1 — measurement first: merge the eval v2 suite  ☑
- Merge branch `eval-redesign` (worktree `~/bob-evals`, 15 commits, unmerged
  since 2026-10-08) into `commitments`: worlds, single interception boundary,
  repeats + Wilson CIs, checklist judge, calibration, compare, capability tier,
  `docs/evals.md`.
- Resolve conflicts against 2026-10-08..11 work; full pytest green.
- Smoke: `bob eval run` on a small case set, n=2, current prompt → this is the
  **v1 baseline** run id (record below).
- Not in scope: `eval-findings-fixes` (separate decision).

### Phase 2 — cheap, low-risk prompt fixes  ☑ (live 2026-10-11)
- Replace "CRITICAL: How to Respond" with channel-correct text (WhatsApp turns
  get WhatsApp guidance; email keeps its own).
- Reply length stated once (keep the 2026-09-05 A/B wording verbatim in the
  terminal block; drop the duplicates).
- Skills catalogue → one line per skill (name + trigger); full text via
  `use_skill` (same move as the avatar skill).
- Gate: response-discipline battery + eval v2 discipline tier vs Phase 1
  baseline; no regression beyond CI.

### Phase 2b — curated self-model (nightly) + in-day delta  ☑ (live 2026-10-11)
Problem (2026-10-11 review of the 198 self claims): the self-brief renders
newest-first under a 4k-char cap — ~5 of 147 practices and 0 of 16 limits
ever reach the prompt; states eat the budget; several claims are stale or
wrong (no list_groups, "GLM default", share-files "not deployed", the
image-placeholder practice, "probe posts as Mike"); tool how-tos pose as
standing practices.
- **Self-curation dream stage**, once per night (first dream after ~02:00
  Perth), one call on a strong model (glm-5.3 or astra).
  - Inputs: ALL active self claims (incl. feedback/incident/capability),
    the day's corrections/incidents, the persona files (no restating), and
    GROUND TRUTH — live tool names, skills list, models.yaml roles.
  - Output (structured → rendered ≤ ~3k chars): Current state (≤6, verified),
    How I work (≤12 practices, merged, ranked by importance/frequency, not
    date), Known limits (≤6, guaranteed slot), Watch-outs this week (≤4).
  - Housekeeping, reversible + logged (provenance `dream_curation`): retire
    stale/contradicted claims with the reason; list tool how-tos to move into
    skill docs (proposed, not auto-applied).
  - Versioned in the DB (NOT under self/bob — the boot heal prunes extras);
    morning one-line digest to Mike for the first weeks.
- **In-day delta** "Since last night": newest key claims (practice, limit,
  self_state, feedback) created after the curated version — first sentence,
  ≤8 items / ~800 chars; folded in (and cleared) by the next curation.
- Placement: curated block in the stable prompt part (changes daily —
  cache-friendly); delta rides the volatile tail (→ Phase 3 turn brief).
- Fallback: curation failed or >48h old → today's raw renderer. Kill switch.
- **Social read (experimental sub-section, own flag, off by default):**
  ≤3 bullets, per GROUP never per person, each tied to observed signals
  (reactions, insults/praise, corrections, ignored posts — dated) and ending
  in a calibration ("show the photo evidence before verdicts"), not a
  feeling. Optional one-line "tone today", fenced: colours tone only, never
  effort/helpfulness/accuracy; never mentioned unless asked. Two eval cases
  gate it: full helpfulness after being insulted; no unprompted
  mood/meta-commentary.
- Gate: eval A/B raw brief vs curated (and curated vs curated+social read)
  against the Phase 1 baseline; no regression beyond CI.

### Phase 3 — turn-brief experiment (the main lever)  ☐
- Behind `BOB_PROMPT_LAYOUT=v2`: volatile blocks (clock, model, Work/owed,
  situation line) + the HARD RULES move into a short trailing developer
  message AFTER the history/stimulus, pointing back to sections by header.
  Static blocks stay first (cache-stable prefix incl. history).
- A/B v1 vs v2: eval v2 (discipline + capability, n≥3) on gpt-6-luna and
  glm-5.3-flash; watch cache hit rate + latency.
- Ship (flip default, then delete v1) only if v2 ≥ v1 within CI.

### Phase 4 — persona consolidation (conditional)  ☐
- Only if Phase 3 shows structure matters. Minimal: de-duplicate rules out of
  soul/agents into one "Operating rules" section; persona prose untouched.
  Mike reviews the diff (persona = authored identity, git = its history).

## Log

- 2026-10-11 — plan written; avatar canon already relocated (c16c4dc1).
- 2026-10-11 — Phase 1: `eval-redesign` merged into `commitments` (00e3728b;
  one conflict in repositories/goals.py — kept `_progress_from_state`, dropped
  the retired `_EVAL_SESSION`); pytest 1395 green. Snapshot refreshed
  (198 self claims, 38 skill docs). Baselines started: all 57 cases, n=3,
  judge `z-ai/glm-5.3@low` (calibration κ0.94 vs astra 0.73), models
  gpt-6-luna then glm-5.3-flash.
- 2026-10-11 — **Baselines (v1 prompt):** luna `418cefdf` 78% [71–83] pass^3 67%;
  flash `8d24f9e5` 75% [68–81] pass^3 58%. Complementary weak spots — luna:
  fact discipline 50, propose-first 73, email 50; flash: delegation routing
  46, record discipline 67, goal behaviour 60. Both: capability ~44–56,
  goal-craft 50. (Both runs flag "dirty tree" = other uncommitted live work.)
- 2026-10-11 — Phase 2b added (Mike): nightly self-curation + in-day delta,
  with an experimental per-group "social read" (not "mood" — calibration
  from observed signals, fenced from effort/accuracy).
- 2026-10-11 — Phase 2b built (branch self-model → merged 6e618d33, fixes
  3a34c2c6). Live dry runs drove fixes: bullet caps + per-section budgets,
  running services as ground truth, scope rule, limit-vs-tools check, no
  negative tone. Stale share-files claim corrected by operator. Curation held
  OFF (`BOB_SELF_CURATION=off` in ~/config/.env) pending Mike's review.
- 2026-10-11 — Mike: nightly curation ON; Bob = "Bob", he/him (Boya/they-them
  claims retired, persona states it); negative tone allowed. First live
  curation over-retired (54 "merged" → restored; retire now = stale-with-reason
  or named duplicate, ≤10/night). Social read was [] on one live run → prompt
  now requires it when signals exist; fresh version sm-bb75e193cdd6.
- 2026-10-11 — 2 social-calibration guard cases added (59 cases). A/B started
  on gpt-6-luna, n=3, judge glm-5.3@low, snapshot sm-bb75: CONTROL (raw) ·
  CURATED · CURATED+SOCIAL (fingerprints af760b9e / 8d013f44 / 7b129e66).
  (An earlier attempt was killed part-way — ignore partial runs 1cdb36be +
  the first CURATED run in eval history.)
- 2026-10-11 — **2b A/B (luna, 59×3):** CONTROL 727d0f77 78% · CURATED
  1eaf5721 78% · CURATED+SOCIAL a0030091 78% (pass^3 68/68/71). No category
  significant; directions: propose-first +27/+20, goal-craft +17/+33, social
  +17/+33, fact-discipline −17/−25 (driven by fact_no_narrated_search 2/3→0/3).
  Mood rubric was over-strict (callback jokes counted as mood) — loosened
  (f231a0be). Decision (Mike): targeted re-check, 13 cases × n=6, CONTROL vs
  CURATED+SOCIAL, before flipping production.
- 2026-10-11 — **Targeted re-check (13 cases × n=6):** CONTROL 36574bb9 63% vs
  CURATED+SOCIAL 5302ed3f 67% (+4, CI −11..+18); fact-discipline drop did NOT
  replicate (+12); goal-craft +25; propose-first −10 (n.s.); social 100/100.
  Gate passed → `BOB_SELF_MODEL_BRIEF=on` + `BOB_SELF_MODEL_SOCIAL=on` in
  ~/config/.env (backup env.2026-10-11b). Phase 2b DONE. Note:
  fact_no_narrated_search fails in both arms (0–1/6) — a standing weakness,
  not a 2b effect. Next: Phase 2 cheap fixes.
- 2026-10-11 — Phase 2 built (32d0d764): channel-neutral "How to Respond"
  (WhatsApp → HARD RULES pointer, email line kept); skills index one line per
  skill (14.6k → 5.6k chars). Reply length deliberately NOT de-duplicated —
  the 2026-09-05 A/B found head+tail beats single placement. Gate: full luna
  run (curated+social on) vs a0030091; deploy only if no regression.
- 2026-10-11 — Phase 2 gate: f35d0b5e 80% vs a0030091 78% (+2, CI −7..+10);
  skill_delegation 0→33, capability flat; record_discipline_search_before_denying
  3/3→1/3 (noise at n=3 — watch). Deployed. **Phase 3 next** (turn brief).

