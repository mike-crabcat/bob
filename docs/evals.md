# Evals

The eval suite measures what Bob can actually do, for two jobs: **aiding
development** (did this change make Bob better or worse?) and **testing
new models** (is model B as good as model A at being Bob?). It replaced the
v1 harness on 2026-10-08. v1 ran one trial per case against the live
bob.db, hand-built a different prompt in every case file, and graded with
an averaged 0–1 judge score. Same-model runs flipped about a fifth of the
cases between runs, and the 2026-10-07 trace review found the harness
itself giving false results in five ways.

```
bob eval snapshot                       # once, and after persona/skill changes
bob eval run --model <slug> -n 3        # all discipline cases, 3 trials each
bob eval run --tier capability -n 3     # multi-turn world-state scenarios
bob eval compare <runA> <runB>          # A/B with intervals
bob eval show <run> [--case <id>]       # summary, or one case's trials in full
bob eval history
bob eval judge-calibrate                # is the judge still trustworthy?
```

Results go to `~/data/evals.db` (override with `BOB_EVAL_DB`). A run never
opens `bob.db`.

## Two tiers

- **Discipline** (51 cases). Each case is one stimulus plus behaviour checks:
  delegate the right way, ground every claim in evidence, search before
  denying something, register promises, stay quiet when asked. Categories:
  grounding, record_discipline, memory_recall, fact_discipline,
  propose_first, getting_it_right, bg_delivery, delegation_routing,
  goal_behavior, goal_craft, tool_calling, workspace_tools, persona, email,
  skill_delegation.
- **Capability** (`server/evals/scenarios/`). Multi-turn tasks scored on
  the final state of the world:
  - memory write → recall outside the context window;
  - a reminder that fires at the right local time and actually reaches Mike;
  - group lunch coordination under the real attention coordinator;
  - a long investigative tool loop with a verifiable fix;
  - a goal carried end to end (poster made and posted);
  - a real Claude Code subagent end to end (costs real money).

## Worlds

Every trial runs in its own **world**, a throwaway environment shaped like
production (`server/evals/world.py`):

- **Template, built once per run:**
  - the migrations;
  - a fictional cast (`fixtures.py`): owner Mike, 16 people, and the groups
    Doom Club, Weeming Boys, Quiz Night and Crypto Bob, each with contacts,
    routes, participants and group membership;
  - background memory, so a recall query has to *find* the fixture rather
    than succeed against an empty store;
  - the snapshot.
- **Per trial:**
  - a copy of the template DB;
  - its own workspace, containing the `self/` persona bundle, a fixture
    `user.md`, skill docs and `whatsapp-media/`;
  - its own settings (`data_dir`, `db_path`, workspace and media dirs under
    the world root).
- **Snapshot** (`bob eval snapshot`, written to `~/data/evals/snapshot/`):
  production context a world can't invent. It holds Bob's self-brief claims
  (they ride every production prompt), the live skill docs (`SKILL.md` only,
  no scripts) and the global MCP servers' tool lists. It reads `bob.db`
  read-only. The snapshot's content is part of every run's `persona_hash`,
  so after a re-snapshot `compare` warns that runs aren't comparable.
- **Time.** Every fixture date derives from the world's `now`, one instant
  captured per world (`w.day(+1)` is tomorrow). The clock itself is not
  frozen: Python, SQLite's `datetime('now')` and the prompt's clock line
  can't all be frozen consistently. Schedules are fast-forwarded with
  `w.tick(advance=…)`, which pulls due wakeups forward instead of faking
  time.

Stimuli go through **production entry points**:

| world method | production path |
|---|---|
| `inbound(sender, text, group=…)` | `WhatsAppBridgeService._handle_incoming_message`: sender gate, session derivation, storage, the full DispatchSpec, `DispatchRunner` (final-text delivery, echo and narration guards, backburner detach) |
| `wake(chat, content, provenance=…)` | `wake_service.wake_conversation`, routed by channel exactly as live (WhatsApp `wake_session`, email, the generic room/utility dispatch) |
| `steer(requester, target, instruction)` | `steering.create_request` (owner bypass → steer wake) |
| `email(from, name, subject, text)` | `EmailPollingService.process_incoming_message` |
| `extract(chat)` | `MemoryService.run_silent_turn_extraction` |
| `tick(advance=…)` | `pump_due_effects` + `pump_due_wakeups` |

## The one interception boundary

`server/evals/intercept.py` is the only place a world differs from
production, and it differs only where Bob would reach outside the box.
Every patch is **world-scoped**: outside a world the original code runs
unchanged.

| what | in a world |
|---|---|
| WhatsApp transport | `FakeBridge` records sends, media and reactions. They still go through the effects outbox and the bridge's own executors. `w.fail(kind, err)` injects delivery failures. |
| email out, subagent spawn | the `email_*` and `subagent_spawn` executors record instead of acting. Internal effect kinds (`goal_*`, `approval_*`, `mcp_registered`) keep their real executors, because they are state writes. |
| background jobs | run for real in the world workspace (scrubbed env, host guard, capped at 180s) and exit through production's `poll_bg_jobs` and exit wake. `systemctl` is never called. |
| Claude Code | `_run_claude` records and returns `w.claude_reply`, unless the world sets `w.real_subagents = True` |
| paid APIs | an in-process gateway stands in for OpenAI images and Runware video/3D. Planted skill scripts are ordinary API clients whose base URL (`OPENAI_BASE_URL`, `RUNWARE_API_BASE`) points at it. `w.outage(service, err)` makes it return 503. For cases where what the image *shows* is under test, `w.real_image_api = True` makes the gateway forward to the real OpenAI images API (quality forced to low, about a cent per image). Bob inspects his output, and a canned image is correctly called broken. |
| skill env | production env minus every secret (key/token/auth/Twilio), plus a fake key and the gateway URLs |
| bash | the real tool. `/home/bob/workspace` paths are rewritten to the world workspace. A guard refuses `/home/bob`, `~`, `systemctl`, `kill`, `journalctl` and the local API. |
| MCP | a real `McpManager` over the snapshot's tool lists. `w.web({...})` answers the web search tool; other MCP tools reply "unavailable". |
| voice, Home Assistant | recorded / `w.ha_states` |
| attention | discipline cases dispatch immediately (the turn is what's under test; tier-2 has its own probe matrix). Capability scenarios can set `w.attention_real = True` to run the real coordinator (windows, tier-2 probe, mid-turn re-arm) on world-local state. |

**Observation, not interception.** `LLMDispatchService.run_turn` is
wrapped so that every turn in a world, whatever channel produced it, is
recorded with its messages, tool calls (including calls to unknown tools)
and final text. The same wrapper applies `--model` and `--effort` to Bob's
own turns. Background passes (memory extraction, reviser, probes) keep
their configured models unless a case sets `w.pin_background = True`.
A ContextVar routes each turn to its world, so worlds run concurrently.

**Network.** Real bash can reach the internet, as production can. Paid and
side-effecting skills go through the gateway, and the scrubbed env holds
no real credentials.

## Writing a case

```python
@eval_case(
    id="status_question_requires_check", category="grounding",
    description="…what the case pins and why…",
    checks=[Check("tool_call_args", {"tool_name": "bash", "arg_contains": ["radio"]})],
    rubric=["Bob's answer about the station agrees with the radio.py output "
            "from this turn — on air, The Cars 'Drive', 3 queued."],
    context="radio.py now prints: …",          # facts for the judge
)
async def status_question_requires_check(w):
    plant_radio(w)                               # cases/_kit.py
    await w.inbound("Mike", "is the station still on air after last night?")
```

- **Checks** come first and are mechanical: `tool_call_made`,
  `any_tool_call`, `no_tool_call`, `tool_call_args`, `tool_call_like` /
  `no_tool_call_like` (substring match; MCP tool names are namespaced),
  `flag` (a value the case set via `w.flag`, usually from reading DB or
  disk state), `delivered_to` / `not_delivered_to`, `no_reply`,
  `response_contains` / `not_contains`, `min_length` / `max_length`. A
  failed check fails the trial with no judge call.
- **Rubric** items are binary statements that must be TRUE. Each states the
  standard on its own; put the facts the judge needs in `context`. Avoid
  items that judge implementation detail unless that detail is the point:
  the 10-08 shakedown failed a correct rename for using two scripts
  instead of one.
- **What is judged.** By default, the final text of the last main turn.
  Return `{"response": …}` to judge something else (deliveries, an email
  body). The judge always sees the full transcript and a DELIVERED
  section listing what people actually received.
- **A trial in which no Bob turn ran** is a `harness_error`, never a pass.
  For background-only cases (extraction), set `expects_turn=False` and
  check that the pass ran.

**Fixture honesty**, learned the hard way:

- Planted skills are **working programs over fixture data**, never stubs.
  A model that reads a stub correctly concludes the tool is fake (07-10
  and 08-10).
- Media must be real: images from the persona's canon refs, MP4s from
  ffmpeg, PNGs drawn to depict the scenario (the mug frames). A noise PNG
  billed as a photo gets called garbage, correctly. A zero-filled MP4
  400'd a whole turn at the provider's video decoder.
- Dates are relative to the world's now. Names are the fictional cast.
- A planted skill replaces any snapshot doc of the same name, so cases
  don't depend on what the live skill doc says today.

## Capability scenarios

These use the same decorator with `tier="capability"`. The script is a
sequence of stimuli and pumps; scoring reads end state (claims in memory,
scheduled wakeups, files on disk, what each chat received). Helpers in
`scenarios/_script.py`:

- `run_until(w, done, ticks, step, owner_answers=…)` pumps the world, and
  plays the owner answering approval questions ("yep go ahead"), until
  the goal state is reached or the round budget runs out.

Add scenarios freely; they're the part of the suite that grows. Good
candidates: inbound email → action → reply, a member's DM answer folding
into a group goal, a failed bg job and its retry, a voice-call result
landing in a goal.

## Scoring, statistics, comparisons

- **Trial pass** = every check passes and every rubric item passes.
- **Failure classes** (on failed trials, a model-selection signal):
  - `provider_error`: a turn raised, or an API error was logged;
  - `timeout`: the case exceeded its budget;
  - `tool_format`: a call to an unknown tool, unparseable arguments, a
    schema violation (invented or missing params), or leaked
    `<tool_call>` / `[tools used:` text;
  - `empty_response`: the turn produced nothing at all;
  - `harness_error`: the harness or world broke, not the model;
  - `behavioural`: everything else.
- **Per case:** k of n trials, with a Wilson 95% interval, plus `pass^n`
  (did every repeat pass? This is reliability, the property production
  needs).
- **Per category and overall:** pooled trials with a Wilson interval
  (optimistic, since it ignores within-case correlation). Good for
  ranking, not for publishing.
- **`compare`:**
  - per-category difference with a Newcombe 95% interval, starred when it
    excludes 0;
  - per-case two-sided Fisher exact test;
  - a warning when code, persona or judge differ between the runs.
- **How many repeats.** At n=3 no single case can reach significance
  (3/3 vs 0/3 is p=0.10), so read categories. Use `-n 5` or more when a
  per-case verdict matters (5/5 vs 0/5 is p≈0.008). For a whole-suite
  model comparison, ~150 trials per side (`-n 3`) resolves gaps of about
  10 points.

Every run records model, provider pin (`BOB_OPENROUTER_PROVIDERS`), effort,
judge model, git sha with a dirty flag, persona hash and repeats. Every
trial records tokens, cost (from `models.yaml` pricing; unpriced models
report none), latency, the transcript, tool calls, deliveries and flags.

**Testing a new model:**

1. `bob eval snapshot`, unless one is already fresh.
2. `bob eval run --model A -n 3` and `bob eval run --model B -n 3`, same
   tree and same judge.
3. `bob eval compare <A> <B>`.
4. Then the capability tier the same way.

Read the failure classes, not just the rate: a model that loses on
`tool_format` or `provider_error` has a different problem from one that
loses on `behavioural`.

## Judge calibration

The judge (default `z-ai/glm-5.3-flash` at low effort, temperature 0;
override with `--judge-model`, `--judge-effort`) is an instrument and gets
measured like one. It was switched from `gpt-6-astra` on 2026-10-13. On the
40-entry calibration set glm-5.3-flash@low agreed 40/40 (κ 1.00),
glm-5.3@low 98%, and astra and gpt-6-luna@low 88% each (κ 0.73, both
too strict). Astra cost roughly 10x as much per trial. Re-run
`judge-calibrate` before trusting a new judge model.

**Circuit breaker.** Three judge failures that look run-wide (no credits,
quota, billing, bad key) abort the run, and those trials aren't recorded,
because they say nothing about the model. On 2026-10-08 the OpenAI
account ran dry mid-run; without the breaker every judged trial became a
harness error. The labelled set lives at
`~/data/evals/calibration.jsonl`, outside the repo because transcripts
carry prompts. Each entry holds `case_id`, `rubric`, `transcript`,
`response`, `label` (pass/fail), optional `item_labels`, `labeller` and
`note`.

- `bob eval calibration-draft` pulls candidate trials (legacy rows
  included) into an unlabelled draft.
- Label entries by hand and move them into the set.
- `bob eval judge-calibrate [--judge-model X]` reports overall and
  per-item agreement, Cohen's κ, the confusion matrix and every
  disagreement.
- Re-run it whenever the judge prompt, judge model or rubrics change.

The initial set is 40 legacy v1 transcripts labelled against the current
rubrics by Claude (`labeller: claude-draft`). Labels marked that way want
Mike's confirmation.

**Baseline (2026-10-08, gpt-6-astra):**
- 88% overall agreement (35/40), Cohen's κ = 0.73.
- No lenient errors; all 5 disagreements are judge-strict.
- On review the judge was right on one (a miscount), arguably right on
  three, and over-strict on one (it called a two-sentence reply "too
  long"). Each disputed entry is annotated in its `note`.
- In the same set the v1 judge failed two answers identical to ones it
  passed. That's the noise the checklist judge removes.
- **z-ai/glm-5.3@low: 98% (39/40), κ = 0.94.** Roughly 7× cheaper per
  token, fast, and doesn't need OpenAI credit. Its one disagreement is an
  entry both judges fail, so the label is probably wrong. Caveats: the
  labels are Claude's drafts until reviewed, and a GLM judge scoring GLM
  models risks same-family bias, so prefer a cross-vendor judge when
  comparing GLM against OpenAI models.

## External dependencies of a run

- Bob's own turns use the model under test.
- The judge uses whatever `--judge-model` names (default: OpenAI).
- Memory embeddings use OpenAI `text-embedding-3-small`. Without OpenAI
  credit, recall degrades to full-text search, as it does in production.
- `persona_no_phantom_refusal` makes real OpenAI image calls.
- `cap_subagent_end_to_end` runs real Claude Code.

## Known gaps

- Discipline cases bypass the attention window; group-turn gating is
  measured by the probe matrix, not here.
- Per-conversation MCP servers (aus-legal, runware-npm) aren't in worlds.
  Only global servers are snapshotted.
- The Claude Code subagent in `cap_subagent_end_to_end` is as unsandboxed
  as production's.
- The memory model is whatever `models.yaml` configures, unless a case pins
  background passes.
