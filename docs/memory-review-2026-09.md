# Memory System Performance Review — September 2026

**Date:** 2026-09-12 · **Scope:** live primary instance (`/home/bob/data/bob.db`, journalctl, code as of master + working tree) · **Method:** DB forensics + journal analysis + code reading. Design reference: `docs/memory.md`.

---

## Scorecard

| Area | Verdict | Headline |
|---|---|---|
| Recording (extraction) | 🟢 Healthy | All WhatsApp/email sessions extracted after ~5 min idle; ~139 claims/day in Sept, best month ever |
| Volume retained | 🟢 Healthy, 🟡 churny | 633 active entities, 3,797 active claims; 43% of all claims superseded — heavy but functioning curation |
| Reconciliation | 🔴 Degraded since Aug 29 | Hourly task runs, tool-side repairs apply, but **~75% of final-response parses fail** (GLM switch fallout) — issues/questions dropped, same entities re-reconciled hourly |
| Recall tool usage | 🟡 Sparse | `recall` used in ~1.7% of Sept WhatsApp turns vs the prompt's "ALWAYS use recall/find first"; no way to audit result quality |
| Semantic recall tier | 🔴 Dead | Embeddings table has **1 row** vs 683 FTS rows; every recent embed attempt 400s on the 8192-token limit and errors are swallowed |
| Observability | 🔴 Poor | Recall results never logged; `memory_search_log` is dashboard-only (last entry Jun 19); recon "0 op(s)" summary is a structural lie |

---

## 1. What is being recorded

**Pipeline:** silent-turn extraction (idle heartbeat ≥5 min, `heartbeat.py` → `run_silent_turn_extraction`) runs an LLM agent loop with additive-only tools (`list_entities`, `get_entity`, `create_entity`, `add_claim`), writing typed claims with per-message provenance. The legacy bulletin pipeline (25% of all-time claims) was removed at v7; its rows remain as history.

**Inventory (2026-09-12):**

| Table | Rows | Notes |
|---|---|---|
| `memory_claims` | 6,702 | active 3,797 (56.6%) · superseded 2,899 (43.2%) · retracted 6 — the other 6 statuses (`expired`, `disputed`, `redundant`, …) are never written |
| `memory_entities` | 661 | 633 active; person 78, task 157, file 99, location 62, event 40 … |
| claim types | 90 used | of ~97 declared (3 keys shadowed by duplicate declarations in `claim_types.py`) |
| `memory_questions` | 92 | 57 open · 26 answered · 9 dismissed |
| `memory_entity_mentions` | 844 | only 48 of 638 all-time conversations covered |
| `memory_aliases` | 16 | thin, for a primary recall-resolution tier |
| `memory_entity_relations` | 0 | vestigial — no writer exists (relations live as entity-ref claims) |
| `memory_entity_embeddings` | **1** | effectively dead — see §5 |

**Claim provenance:** extraction 3,667 (55%) · legacy bulletin 1,662 (25%) · tool/answer/correction/contact-sync 799 (12%) · reconciliation-authored 574 (9%).

**Volume trend** (claims created): May 445 → Jun 2,410 → Jul 515 → Aug 1,662 → **Sep 1,670 in 12 days** (~139/day, pacing ~4,200 — the strongest month yet, driven by radio/merch/frigate activity).

**Coverage (September):** extraction ran on 38 sessions — every active WhatsApp (19) and email (18) conversation, plus one frigate utility session. 140 subagent and 41 internal conversations are excluded by design (candidate query filters `subagent:%` and machine provenances). Within its intended scope, coverage is effectively complete: 701 extraction runs in the last 7 days, all status "ok", 47% recording zero claims (normal — most chatter isn't memorable).

**Quality spot-check:** good. Typed, specific, sourced claims — e.g. David: `drink_preference: "tea — milk-first, one sugar"`, `workplace: "CPP, Elder Street, Perth"`; relationship entities carry dated `memorable_interaction` entries including unflattering ones (Bob's own Aug 29 memory-leak incident). Extraction skip-rules (only others' messages, corroboration for synthetic turns) appear to be holding.

## 2. How much is being remembered

- **Per entity:** avg 10.1 claims ever, 6.0 active per active entity.
- **Retention varies sharply by subject:** trip/daylog entities retain 66%+ active; `self-bob` 46%; but **person entities retain 9–21%** (Mike 50 active of 538 ever, Sean 26/126, David 22/121, Blair 28/144).
- Interpretation: person facts get re-extracted in evolving forms and the old version superseded — correct behavior, but it means ~5× write amplification on hot subjects. Observed live: the "merch marketing direction" shared-context claim was extracted and superseded three times inside 36 hours as the conversation iterated.
- **Supersede machinery works:** ~40–60 claims/day superseded, 2,754 rows carry supersede links, zero multi-hop orphans (no claim superseded by an already-superseded claim). Identical-claim dedup merges provenance instead of duplicating.
- **Questions are the pressure valve and they're good** ("Birthday 2026-07-09 — date of birth or anniversary?", "Group 'Bob Blair Brad' has no Blair as member") — but see §4: new question flow collapsed after Aug 29.

## 3. Is reconciliation working?

**The machinery runs.** Hourly `MemoryReconciliationTask` (651 runs/30d, 24 in the last 24h), 4–14 entities per batch, ~2 min per batch. 453 entities reconciled at least once; 144 in September; 70 in the last 48h (consistent with the 50/day batch cap + 6h backoff). Repairs are real: 574 reconciliation-authored claims (~15–30/day), merge tool available, FTS re-rendered after each pass (all 10 entities created since Sep 11 are already in FTS — freshness lag is bounded to hours).

**But it is degraded since the Aug 28 model switch, in three ways:**

1. **~75% of per-entity runs fail to parse the final JSON** (`Reconciliation: failed to parse final response`): 85 failures vs 29 OK in 24h; 550/7d; 1,315 total — and **zero occurrences before Aug 29 08:23**, i.e. it began the morning after the OpenRouter/GLM switch. Tool-side repairs still apply during the loop, but the final `issues/questions` payload is dropped — **questions raised collapsed to 6/24h**, and the same entities are re-reconciled hourly without converging (`group-crypto-bob` ×18, `relationship-bob-david-shedden` ×15 in 7d). This is the GLM-5.3 JSON-output quirk seen elsewhere (claim-router probes, the Sep 9 `JSONDecodeError` task failure) — the reconciliation prompt needs the same rescue/retry treatment the main dispatch got.
2. **Tool-call signature errors:** 7/7d — `supersede_claim_tool() missing 1 required positional argument`, `retract_claim() got an unexpected keyword argument`. The model calls recon tools with wrong shapes; they throw and are swallowed.
3. **Monitoring is misleading:** the heartbeat summary always prints `0 op(s)` because `operations_applied` is structurally always `[]` (effects are live tool calls, `reconciliation.py:822`). Anyone watching that line concludes reconciliation does nothing.

**Backlog:** 187 active entities have never been reconciled, including every exact-duplicate display-name pair found ("ai doom mug" ×2, "bob" ×2, "aussie bbq playlist logo" ×2, …). Automated merge detection depends on embeddings — which are dead (§5) — so these dupes persist indefinitely.

## 4. Is the recall tool being appropriately used?

**Usage in live conversation is sparse.** September, chat turns only (background loops excluded — all 4,892 `get_entity` calls since Aug 1 came from extraction/reconciliation loops, not chat):

| Tool | WhatsApp turns (1,576 total) | Share of turns |
|---|---|---|
| `recall` | 26 turns / 29 calls | **1.7%** |
| `find` | 14 turns | 0.9% |
| `remember` | 15 turns | 1.0% |
| `memory_correct` | 2 turns | 0.1% |

The system prompt instructs "ALWAYS use recall/find before answering…" (`prompt_assembler.py:214-287` injects this guidance into every dispatch). Observed behavior ignores it — Bob leans on in-context conversation history instead. For dense group chats with regulars that's often fine; for cross-conversation facts (David's car, booking details) it's a latent failure mode. Usage rate also drifted down: ~4.8 `recall` calls/day in Aug → ~2.7/day in Sept.

**Mitigating factor:** email dispatches inject a compact entity roster into the prompt for trusted sessions; WhatsApp/voice inject nothing.

**Quality of usage is unauditable:** `recall`/`find` log neither invocation nor results in the journal; `memory_search_log` is written only by the dashboard API (84 rows, all June 3–19, NULL session keys — manual testing). We cannot answer "did recall return the right entity?" from any persisted data.

**Resolution path health** (`recall` = entity_id → alias → embedding top-5 → FTS): the alias tier is starved (16 aliases total); the embedding tier is empty (1 row) so every natural-language query falls through to FTS — which works, but is keyword-brittle.

## 5. The embedding tier is silently dead

`memory_entity_embeddings` holds **1 vector** against 683 FTS-rendered entities. The write path (`update_entity_fts`, `claim_service.py:428-477`) does FTS DELETE+INSERT (works) then `embed_text` + `upsert_embedding` inside `try/except Exception: pass`. What's observable:

- ~2–4 embed attempts/day reach the API; **every logged failure is a 400** — `maximum input length is 8192 tokens`. Three entities have rendered bodies over the limit (46k/41k/38k chars) and fail on every recon pass.
- No truncation exists (`embed_batch` sends the full rendered body; one oversize item kills the batch).
- Successes are unlogged and, judging by the 1-row table, not landing — the only silent failure point left is `upsert_embedding` inside the swallowed `except`, or returns of `None`; nothing distinguishes these today.
- Consequences: semantic `recall` tier returns nothing; `merge.py` embedding-based duplicate detection has no candidates — one likely reason the never-reconciled duplicate pairs persist.

`BOB_OPENAI_API_KEY` is set and vec0 loads correctly on every pooled connection (verified by scratch insert with the same DDL), so this is fixable in code, not config.

## 6. Cost & latency

| Category (Sept) | Calls | Avg tokens | Total | p50 / p90 latency |
|---|---|---|---|---|
| `memory_silent_turn` | 1,049 | 55,425 | ~58M | 23.6s / 74.1s (max 1,150s) |
| `memory_reconciliation` | 1,737 | 14,402 | ~25M | ~24s avg |
| `claim_router_probe` (goal system) | 884 | — | — | — |

Extraction prompt size has improved three-fold (Jun avg 132k → Aug 75k → Sep 55k tokens; tool-loop folding helped) but remains the dominant spend — a zero-claim extraction of a casual group chat still replays ~44k tokens. Reconciliation is burning extra calls on non-converging entities (§3.1). Models: silent turns now exclusively `z-ai/glm-5.3-flash`; recon mixed GLM-flash / gpt-5.6-luna.

## 7. Incident timeline

| Window | Event | Impact |
|---|---|---|
| Jun 3–19 | Manual dashboard memory testing | 84 search-log rows; none since |
| Aug 14–28 | OpenAI credit exhaustion (`429 insufficient_quota`) | **2,870 extraction turns failed (71% of Aug attempts) + 281 recon task failures — ~2 weeks of conversations never extracted, unrecoverably** (cursor advances past failures) |
| Aug 28→29 | OpenRouter/GLM model switch | Recon final-parse failures begin immediately: ~75% since, question flow collapsed |
| Last 7d | Claim-router probe `JSONDecodeError` ×19 (accelerating) | Fails **open** → goals over-notified |
| Ongoing | Embedding 400s ~4/day | Semantic recall + merge detection dead |

## 8. Findings & recommendations (ranked)

1. **🔴 Fix reconciliation final-response parsing** (since Aug 29): apply the GLM JSON-rescue/retry pattern to `RECONCILIATION_PROMPT` output, or have recon emit issues/questions via a tool call instead of a trailing JSON blob. Restores question flow, stops hourly re-reconciling of ~15 hot entities, cuts ~25% of recon spend.
2. **🔴 Repair the embedding write path:** truncate rendered bodies to a safe prefix (or chunk) before `embed_text`; log successes/failures in `upsert_embedding` instead of `except: pass`; run the CLI `rebuild_embeddings` once fixed. This resurrects semantic recall and merge detection in one move.
3. **🟠 Backfill the never-reconciled 187** (incl. all duplicate-name pairs) — either raise `BOB_RECON_DAILY_BATCH_MAX_ENTITIES` temporarily or run `bob memory reconcile` on the backlog; dedup manually via `bob memory merge` where obvious.
4. **🟠 Reconcile the recall policy with behavior:** either soften "ALWAYS use recall/find first" to when-to-use guidance, or make recall cheaper to reach (e.g. inject a per-conversation participant/entity roster on WhatsApp like the email path already does — participants are the hot entities in group chats).
5. **🟠 Instrument recall:** log query → resolved entity → result count (the `memory_search_log` table already exists; wire the tool path to it). Without this, recall quality is unmeasurable.
6. **🟡 Fix recon tool-call signatures being model-hostile** — 7 TypeErrors/week; consider lenient argument parsing in `make_reconciliation_tools` wrappers (known LM enum/arity behavior, see the alias-normalisation precedent).
7. **🟡 Claim-router probe failures fail open** — 19 bad probes/7d deliver stimuli to goals unfiltered; add retry/repair before defaulting to relevant.
8. **🟡 Hygiene:** make `operations_applied` real (or drop the "N op(s)" from the heartbeat line); de-shadow the 3 duplicate claim-type keys; delete the dead `BOB_MEMORY_EXTRACTION_MODE` env var and stale comment; decide whether `visibility`/`scope` should ever be filtered in recall (currently never enforced — latent privacy gap, harmless in a single-owner deployment); extract the extraction glossary cost (55k-token silent turns) further — trim candidate-entity block and glossary to types detected in text (`_detect_entity_types_in_text` already exists).

---

### Evidence commands (reproducible)

```bash
sqlite3 -readonly /home/bob/data/bob.db "SELECT status, COUNT(*) FROM memory_claims GROUP BY status"
sqlite3 -readonly /home/bob/data/bob.db "SELECT substr(created_at,1,7), COUNT(*) FROM memory_claims GROUP BY 1"  # use explicit expr in GROUP BY
sqlite3 -readonly /home/bob/data/bob.db "SELECT COUNT(*) FROM memory_entity_embeddings"                       # 1
python3 - <<'EOF'   # chat-turn-only memory tool usage
import sqlite3, json, collections
con = sqlite3.connect('file:/home/bob/data/bob.db?mode=ro', uri=True)
rows = con.execute("SELECT tool_blocks_json FROM llm_call_log WHERE call_category='whatsapp_incoming' AND created_at>='2026-09-01' AND tool_blocks_json IS NOT NULL").fetchall()
c = collections.Counter()
for (tb,) in rows:
    for b in json.loads(tb): c[b.get('name','')] += 1
print(c['recall'], 'recall calls')
EOF
journalctl --user -u bob.service --since "7 days ago" | grep -c "failed to parse final response"    # 550
journalctl --user -u bob.service --since "14 days ago" | grep -c "MemoryReconciliationTask"         # hourly, running
```

---

## Addendum — fixes applied 2026-09-12 (same day)

Deployed via the 20:24 and 21:0x restarts; `tests -q` green (914 passed) before each.

1. **Reconciliation final-parse rescue** — new `server/services/llm_json.py` (`parse_llm_json`: strict → fence-strip → substring → truncation-repair by closing open containers). `reconcile_entity` now uses it, **retries once** when the model answered in plain prose (a residual failure class the new head-logging exposed), validates issue/question shapes, and drops `{"question": null}` stubs.
2. **Claim-router probe** — `parse_llm_verdict` (JSON rescue → keyword scan → guarded truncation-prefix match, safe-default first). No more fail-open on fence/prose/truncation shapes.
3. **Reconciliation tools hardened** — `_normalise_tool_kwargs` renames the argument shapes GLM invents (`claim_type`→`claim_type_key`, `old`/`new`, `winner`/`loser`, …), drops unknowns with a logged warning, returns an error string on missing-required instead of raising, and **`operations_applied` is now real** — the heartbeat "N op(s)" line finally measures something. Verified live on a 5-entity sample: `add_claim` owner + 2 `retract_claim` ops recorded and applied.
4. **Embedding write path** — `embed_batch` truncates inputs to 24k chars (the 8192-token API limit was 400-ing the 3 fat entities on every refresh), `update_entity_fts` logs failures instead of `except: pass`. **Correction to §5/§8.2:** the "1 embedding row" finding was partly a measurement artifact — that count read sqlite-vec *chunk* metadata through a module-less CLI. True state: coverage was partial (fat entities failed every pass; silent-failure visibility was zero), not empty. Post-fix: full rebuild embedded **639/639 active entities, 0 misses**; KNN verified working; fat entities (`self-bob`, `relationship-bob-mike-cleaver`) embed via truncation; zero warnings since.
5. **Recall instrumented** — `recall` now logs query → resolved entity(ies) → result count → latency into `memory_search_log` with the calling `session_key` (`recall_with_meta` refactor; `find` left unlogged by design). Recall hit-rate is finally measurable.
6. **Hygiene** — dead `BOB_MEMORY_EXTRACTION_MODE` removed from `config/.env`; stale comment fixed in `prompt_assembler.py`.
7. **Backfill** — all 183 never-reconciled entities reconciled (5-entity sample validated first per the careful-rollouts rule; then 3 parallel CLI shards, ~70 min): **258 ops applied** (176 retracts, 51 adds, 20 supersedes, 6 merges, 4 archives), 0 parse failures, real curation verified (empty dayplans archived, trip cross-links added, mis-typed values fixed, e.g. Notre-Dame `viewpoint`→`cathedral`). One true duplicate pair merged by hand (`location-new:rojiura…` → `location-rojiura…`). **Remaining, deliberately left:** 8 cosmetic-ID entities (`new:`-prefixed, a URL-as-person holding the Chamonix Resort business record, one UUID-person) — valid data, ugly IDs; same-name cross-type pairs (dayplan/daylog, decision/task) are by design, not duplicates.

**Deferred (deliberately):** ~~WhatsApp participant-roster injection~~ (shipped 2026-09-13, see below), recall-policy rewording (prompt changes need the probe-matrix discipline), claim-type de-shadowing (keys are live data), `visibility`/`scope` recall filtering, extraction prompt cost trimming.

### Addendum 2 — memory roster shipped (2026-09-13)

Recommendation 4's scoped-roster half, one day later. `ContextAssembler.maybe_memory_roster` → `memory.build_conversation_roster` (SQL lives in the memory package per the ownership rule — the first implementation in the assembler was caught by `test_sql_ownership`, correctly): group sessions only (DMs already get `person_profile`), participants' person entities via contact_id claims first, then `memory_entity_mentions` for the conversation, most recent first — capped at 15 entities / ~1k chars, **fact counts, not contents**, so depth-seeking still routes through `get_entity`/`recall`.

- **Gating:** conversation policy `memory_roster` (default OFF), `/roster on|off|status` slash command, `BOB_MEMORY_ROSTER=off` kill switch (`Settings.memory.roster_enabled`).
- **Seeded:** `agent:main:whatsapp:group:120363408889690088` (Bobs Pirate Radio — already the heaviest recall user, so the A/B is clean). Roster there: 12 entities, 971 chars — group entity, self-bob, Mike, radio tasks/events/files.
- **Watch:** recall usage rate on the seed group via `memory_search_log` (now instrumented) vs the 1.7% baseline; prompt-cache churn is negligible (roster changes only when entities change).
- Tests: `tests/services/test_memory_roster.py` (6).
