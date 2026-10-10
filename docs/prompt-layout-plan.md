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

### Phase 1 — measurement first: merge the eval v2 suite  ☐
- Merge branch `eval-redesign` (worktree `~/bob-evals`, 15 commits, unmerged
  since 2026-10-08) into `commitments`: worlds, single interception boundary,
  repeats + Wilson CIs, checklist judge, calibration, compare, capability tier,
  `docs/evals.md`.
- Resolve conflicts against 2026-10-08..11 work; full pytest green.
- Smoke: `bob eval run` on a small case set, n=2, current prompt → this is the
  **v1 baseline** run id (record below).
- Not in scope: `eval-findings-fixes` (separate decision).

### Phase 2 — cheap, low-risk prompt fixes  ☐
- Replace "CRITICAL: How to Respond" with channel-correct text (WhatsApp turns
  get WhatsApp guidance; email keeps its own).
- Reply length stated once (keep the 2026-09-05 A/B wording verbatim in the
  terminal block; drop the duplicates).
- Skills catalogue → one line per skill (name + trigger); full text via
  `use_skill` (same move as the avatar skill).
- Gate: response-discipline battery + eval v2 discipline tier vs Phase 1
  baseline; no regression beyond CI.

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
