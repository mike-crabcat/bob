# Changelog

All notable changes to Bob are documented here. Entries are based on analysis of actual code changes, not just commit messages.

## 2026-08-24 – 2026-08-25

### Added
- Add hierarchical goals with living state (Bob Events): parent/child links with a goal↔conversation holder set keyed on canonical conversation ids, child results roll up into the parent's strategy worksheet via a cheap-model reviser instead of waking the origin, child deadline wakeups land on the root's working conversation, and versioned strategy worksheets (plan, known, open questions, next actions, entity refs) replace transcript re-derivation — goal context now injects into prompts on the WhatsApp inbound, generic wake, and email paths
- Add silent goal-state revision: the reviser folds routed claims and child results with decision-rule evaluation (quorum, decide-by), wakes the working conversation only when decisions or next actions actually change, degrades to a wake rather than losing information on failure, and runs behind per-goal serialization, a concurrency cap, and BOB_GOAL_STATE_SHADOW / BOB_CLAIM_ROUTER_DISABLED kill switches
- Route memory to goals across channels: claims extracted in any conversation reach the goals they affect via strategy entity-ref matches, a new entity↔conversation mention index (maintained on every claim write and backfilled by `bob memory mentions-backfill`), and human participant overlap with a relevance probe that fails open; delivery is durable through an event-log watermark with heartbeat replay, echo-suppressed per originating conversation, and every decision audited in a routing log
- Steer cross-conversation entity identity: the silent-turn extractor prompt gains a candidate-entities block seeded from held and participant-overlapping goals so the same real-world thing reuses one entity id, and create_entity steers the model away from exact display-name duplicates
- Extend the goal tool surface: create_goal takes kind, parent, and a validated strategy; new update_goal_state does CAS worksheet writes, schedule_goal_wakeup books reminder wakeups, and list/instantiate goal templates create a full plan DAG (negotiate with decision rules, venue, book, remind, merch) as data overridable from config; goal tools reach the generic wake and email paths, outreach and subagent spawns can parent under plan children, and a policy-gated proactive group-send tool ships off by default per group
- Add the merch pipeline behind a human payment gate: an approvals table (recreated with a purchase type) with request/respond tools where approving a purchase chains exactly one durable order effect whose executor independently re-verifies the recorded approval before contacting the vendor, holds credentials in a server-side file outside the agent's bash reach, and sends vendor idempotency keys so crash retries cannot double-order (BOB_MERCH_ENABLED, off by default)
- Add a progress-review heartbeat loop: goals untouched past a threshold (24h default) get coherence checks that track a stuck streak, wake the working conversation on the second consecutive no-change review, and escalate to the origin at the fourth
- Add dashboard observability for the new machinery: the goals page renders the goal tree with state worksheets (plan, open questions, next actions, entity chips) plus a memory-routing decision feed, backed by tree and state fields on the goals API and a routing-log endpoint
- Add a multi-party rehearsal harness for the benchmark scenario: persona-scripted parties replying across channels including the wrong-channel cases, scripted LLM stand-ins driving the real extraction, routing, reviser, goal, wake, and effects pipeline, a local print-on-demand stub, compressed-deadline time control, and a formal zero-information-loss scorer gated on the deterministic all-replies-in-group run

### Changed
- Make goal settlement the single wake chokepoint: phone-call results ride it with their own content and category overrides, and child roll-ups queue for the effects pump instead of executing inline inside executor call stacks

### Fixed
- Fix wake dispatches silently disappearing: detached dispatch tasks were only weakly referenced, so the scheduler could garbage-collect one mid-flight and drop the wake entirely — they are now strongly referenced until completion
- Fix routed replies fragmenting across the goal tree (template children lacked versioned envelopes and the root's entity refs) and persona-DM extraction minting duplicate entity slugs (parented outreach goals now inherit the parent's refs so candidate seeding reaches the target conversation)
- Fix overdue-goal detection comparing second-truncated timestamps against microsecond-ISO writers
- Make the reviser's per-goal locks and concurrency semaphore loop-aware so a process running multiple event loops cannot inherit primitives bound to a dead one
- Fix the attention probe misreading group transcripts: speaker lines are attributed through participants instead of anonymous "User:" labels, and routine or subagent chatter and NO_REPLY bookkeeping rows no longer pollute the probe's view of the conversation

### Removed
- Remove the dead bulletin pipeline residue: Bulletin models and generator inputs, source_bulletins fields and their constructor sites, memory_entity_bulletins write paths, and the caller-less group-entity helpers — the tables were dropped by migration 353, making those writes latent errors

## 2026-08-23

### Added
- Add a quota circuit breaker on all four LLM dispatch entry points: a 429 quota or credit-exhaustion failure fails subsequent calls fast for a five-minute cooldown (no API request, no call-log row) until the first success closes it, ending the ~1,700-calls-per-hour retry storm observed during an overnight credit outage
- Move routine scheduling onto the unified wakeup pump: each enabled routine holds exactly one wakeup row with cron recurrence computed in the routine's timezone and stored as UTC, claim-first firing prevents a slow run from double-firing, and routine CRUD keeps the schedule in sync
- Canonicalize session identity at ingress: WhatsApp and email resolve their channel-derived session key to a canonical conversation at the seam, so all downstream state keys under the conversation and a merged binding lands in its survivor with no per-call-site changes (resolution is fail-open, falling back to the raw channel key)
- Bind voice calls to conversations: outbound contact calls bind their subagent session to the person's conversation so both phone and voice-link completions record idempotent call.completed events there, and claude/local subagent spawns become durable subagent_spawn effects with re-delivery guards — a failed spawn now marks the subagent failed and returns the error to the LLM instead of the run silently disappearing
- Add a live-call occupancy state machine: while a call is live on a person's conversation, inbound WhatsApp text stays stored-but-undispatched and runs as one post-call turn, urgent text (hang-up or emergency vocabulary) bypasses the queue, concurrent live calls are capped at two before placement, and stale live entries expire after an hour
- Add episode redaction tooling and probe evals to `bob replay`: export-episode samples production conversations into redacted replay fixtures (phone remapping, name aliasing, phone/email/URL masking, operator review still required), export-probe-candidates dumps recent shadow decisions for golden-label curation, and probe-matrix scores the attention probe against golden labels with a live confusion matrix
- Add an operations health strip and needs-attention card to the dashboard home, backed by a new status endpoint surfacing quota-gate state, effects-outbox health, active and overdue goals, scheduled wakeups with next fire time, stuck turns with expired leases, undispatched inbound messages, and database size — dead-lettered effects can be retried or discarded directly from the card
- Replace the dashboard sessions views with conversation-centric pages: a conversations list with channel filter chips, multi-binding and merge badges, and activity ranking, plus a conversation detail page with bindings provenance (per-binding unmerge) and a collapsible decision timeline merging attention-shadow decisions, tier-2 probe reasoning, turns, effects, and goal transitions
- Add a Goals & wakeups dashboard page with expandable active and settled goal cards (progress, result, transition history, link to the owning conversation), a scheduled-wakeups list with recurrence, timezone, and live countdowns, and one-click cancel that settles through goal_service so pending wakeups are cancelled and the origin conversation woken

### Changed
- Canonicalize outbound-initiated email threads into conversations at send time (inbound ingress already did), classify email threads as kind='thread' instead of falling through to internal, and rank the conversations list by LLM call activity so email threads (which have no turn rows) surface at their true recency
- Rename Sessions to Conversations throughout the dashboard (nav, headings, stat boxes, empty states) and redirect legacy /sessions URLs to their /conversations equivalents
- Exclude quota-exhaustion 429 failures from the home 24h call chart and cost-by-category/model aggregates so zero-cost retry storms from a credit outage no longer drown out real activity; the raw rows remain in the LLM call log for audit
- Mark conversations absorbed by a merge with a "→ merged" badge so merged-away threads are identifiable in the list

### Fixed
- Fix memory entity-merge reconciliation raising "'NoneType' object can't be awaited" on every merge by awaiting the scheduling callback only when it returns an awaitable
- Fix recurring wakeups firing continuously when the server clock sits in a different timezone offset than a routine, by computing cron occurrences in the wakeup's timezone and storing them as UTC

## 2026-08-21 – 2026-08-22

### Security
- Add a default-deny API token gate: all state-changing HTTP requests (POST/PUT/PATCH/DELETE) now require the API token or receive a 401, closing the incident path where the agent's own bash tool could POST anonymously to place real Twilio calls; the token is accepted as a Bearer or X-Dashboard-Secret header, dashboard cookie, or ?secret= query parameter, with Twilio webhook callbacks and the public voice-page log sink exempt and BOB_API_AUTH_DISABLED=true as a break-glass switch
- Change the secret's default posture: an unset BOB_DASHBOARD_SECRET now auto-generates a urlsafe token persisted to the data directory (mode 0600, stable across restarts) instead of leaving the dashboard fully open, and is deliberately kept out of os.environ so the agent's bash tool cannot leak it via printenv

### Added
- Add a one-URL dashboard login for browsers and phones: GET /dashboard/api/auth?secret=<token> validates the token and sets a year-long cookie, replacing the hand-set-cookie-in-DevTools flow that was impossible on mobile — the URL tolerates raw-pasted tokens whose base64 '+' arrived decoded as a space, always writes the canonical secret, and reads the token from the URL directly so revisiting it heals a stale poisoned cookie
- Add an assigned_identity memory claim type for playful identities a group has settled on for a member (or for Bob), with extraction carve-outs attributing the identity to its owner and reconciliation rules keeping group-settled identities from being retracted as banter
- Add a daily LLM call-log retention task that strips prompt, message, response, and tool payloads from rows older than 30 days while keeping token, latency, status, and model metrics forever — a one-off backfill shrank the live database from 2.5GB to 691MB
- Introduce a durable append-only event log at ingress (migration 400) with transactional writes: every accepted WhatsApp message, email, phone status webhook, and routine firing is recorded exactly once (unique source + external_id) in the same transaction as its legacy store write, so bridge redeliveries and webhook retries can no longer double-accept; the migration also lays down turn leases, an idempotency-keyed effects outbox with retry backoff and dead-lettering, wakeups, and subscriptions, with a daily legacy-vs-event-log reconciliation audit
- Add an attention coordinator owning WhatsApp dispatch timing: structurally addressed messages dispatch after a 2.5s micro-window, unaddressed group chatter batches for 20s, typing indicators extend the window, and a 90s hard cap bounds latency; an LLM actionability probe (ACT/WAIT/STAND_DOWN) runs at window close for unaddressed group batches only, probe failure falls back to ACT so infrastructure can never cause silence, and BOB_ATTENTION_ALWAYS_ACT=1 is a kill switch — cut over live the same evening it was built (the planned shadow-agreement soak was skipped), making the attention_shadow table the live decision audit trail
- Add durable goals as effects-backed LLM tools (create/update/complete/list) with CAS status transitions and versioned revisions: deadlines schedule wakeups so unanswered goals resurface, and settling a goal cancels its wakeups and wakes the originating conversation with the result
- Add a unified channel-agnostic conversation wake path: subagent results, voice-call results, email-thread results, and outreach completions store their content as an undispatched message and dispatch through the channel's real pipeline, so a crash before dispatch is recovered by the startup sweep
- Add conversations and bindings as the identity layer: channel session keys resolve to canonical conversations via a binding table mechanically backfilled 1:1 (conversation id = legacy session key, so existing event-log history resolves without rewrites), with merge moving bindings to a survivor with merged-from provenance and unmerge returning them to their pre-merge conversation
- Add an effects outbox for all outbound sends: WhatsApp text/media and email replies/new email are recorded durably with idempotency keys before inline delivery (user-facing latency unchanged), and retried with backoff by a heartbeat pump after a crash, so a failed send no longer loses the turn and retries cannot duplicate a delivered effect
- Add a replay harness with a type-enforced fake effect sink: curated episode fixtures replay through real ingress, attention, dispatch, and effects code with a scripted LLM and zero external actions, asserting burst-collapses-to-one-turn, group STAND_DOWN silence, and ACT sends
- Add characterization test suites (~860 lines) pinning WhatsApp and email inbound behavior ahead of the re-architecture, including the unknown-DM-drop vs group-auto-seed asymmetry, trust-gated tools, NO_REPLY semantics, delivered-only history, and failure injection at the send and store-to-dispatch boundaries

### Changed
- Make dispatch turns durable: each dispatch claims a turn row under lease and marks it complete or failed, a startup recovery sweep re-arms dispatch for stored-but-undispatched messages after a crash, and messages arriving mid-turn get their own follow-up turn instead of stranding until the next stimulus
- Write assistant history only from delivery confirmation, so a reply enters history only after its outbound effect actually delivered
- Put subagents and outreach onto goals: a spawn creates a goal held for the parent whose completion wakes the parent on any channel (previously WhatsApp-only relay), killing a subagent cancels its goal, and WhatsApp outreach creates a 24h-deadline goal so unanswered outreach resurfaces automatically
- Centralize contact access behind a ContactRepository with explicit per-channel inbound policies, codifying the pinned asymmetries (WhatsApp drops DMs from unknown numbers while auto-seeding unknown group senders as untrusted; email accepts and seeds every sender), and sweep the remaining ~24 inline contacts queries across 13 service modules onto the repository
- Route all conversation-history reads through a single HistoryRepository and all system-prompt context blocks through a shared ContextAssembler, deleting the duplicated copies in the WhatsApp bridge and email poller; replace the five near-identical channel dispatch closures with a shared DispatchRunner (lock → claim → LLM → tap → history → publish) with per-channel differences made explicit as spec fields
- Load local STT/TTS voice models lazily on the first legacy /voice/ws connection instead of at startup, freeing roughly 8GiB of idle GPU memory now that realtime calls do STT/TTS at OpenAI; clients see a loading status with push-to-talk disabled, failed loads retry on the next connection, and BOB_VOICE_PRELOAD=true restores eager loading
- Restrict the appearance memory claim type to durable physical description only — one canonical description per person, photo-specific clothing at most as "in this photo…" — with reconciliation retracting photo captions, scheduling chatter, and directives, and consolidating duplicates

### Fixed
- Stop the realtime voice bridge interrupting itself on echoey phone lines: barge-in previously fired on bare voice-activity detection, so an analog landline echoing the agent's own audio back caused the agent to chop its opening sentence mid-word (both Broken Hill Hotel calls collapsed this way); the interrupt now waits for a non-empty user transcription confirming real human speech
- Cancel and unwind all realtime session tasks when the bridge tears down on callee hangup, instead of leaking the duration timer (which fired "max duration reached" logs minutes after the call ended) and leaving the end-requested waiter permanently pending

### Removed
- Remove the legacy local voice pipeline (~4,700 lines): the /voice/ws endpoint, local STT→TTS engine stack, browser transport and protocol, voice session store, lesson progress service, and the language-practice frontend — all voice now runs through the OpenAI Realtime bridge, and the faster-whisper/omnivoice dependencies and [voice] extra are dropped
- Remove the patience gate and its 450-line test suite, superseded by the attention coordinator, along with RoutineSchedulerTask's dedicated due/claim machinery absorbed by the wakeup pump
- Remove the WhatsApp-bound subagent result relay, the generic thread_result_service, and the standalone email thread result tool module, replaced by the unified wake path (the finish_email_thread tool lives on inside email_tools)

## 2026-08-16 – 2026-08-18

### Added
- Prewarm outbound-call realtime sessions: the OpenAI Realtime session is connected and fully configured while the phone rings, and the media stream claims it at answer so the callee's greeting flows into a live session instead of riding a setup-backlog burst into a half-configured one
- Detect voicemail on outbound phone calls by matching the callee's opening words against recorded-greeting phrases: the agent leaves one short message and the bridge ends the call after a reply window if nobody speaks, with misdetected live humans self-correcting by replying
- Add a get_session_messages tool so agents and routine runs can re-check what was actually said in a session (bounded, sender-attributed reads) instead of baking volatile facts into prompts
- Add a BOB_PHONE_TWILIO_REGION setting to pin the Twilio client to a home region

### Changed
- Make dream autoplan session-scoped instead of a global runtime toggle: /autoplan sets a per-chat flag so only plans whose evidence came from that conversation auto-approve, the CLI toggles per session with --session, and the dashboard Controls tab lists enabled sessions with individual turn-off (a global boot default remains via BOB_DREAM_AUTO_APPROVE_PLANS)
- Pass static stream TwiML inline when placing outbound Twilio calls, removing the webhook fetch from the answer critical path (the inbound TwiML webhook remains for per-call setup)
- Reject routine prompts that instruct the run to create, modify, or delete routines, since routine dispatch withholds those tools and the instruction could never be obeyed

### Fixed
- Fix call recordings collapsing the call's opening seconds: inbound audio taps are stamped at Twilio arrival time rather than relay dequeue time, and burst-fed frames are laid back-to-back with correct bytes-vs-samples math so they can never overwrite earlier audio
- Hold, instead of cancel, opening-window responses that have no transcription yet, so a real greeting whose transcription races response.created is no longer killed as noise, leaving the caller in dead air — released on human transcript, cancelled on empty transcription or decision timeout
- Restore session visibility for untrusted dispatches with no resolved contact (routine runs in WhatsApp groups), which previously could see no sessions at all — not even the conversation they post into — and worked from stale remembered confirmations

## 2026-08-16

### Added
- Add the dream system: idle-time dream runs review sessions active since the last dream and produce evidence-cited resolutions (self-improvement items, kept only when a success signal is positively observed) and plans (unfinished business detected in conversation, announced once in the session where the evidence was cited), with an auditable per-run journal — all passes on the low-cost memory model, per-run caps with rollover for deferred candidates, and safe defaults (disabled, draft mode, nothing announced)
- Add `/autoplan on|off|status` for trusted WhatsApp contacts, with dashboard and CLI equivalents: runtime auto-approval of dream plans that never triggers outbound outreach and is blocked for plans mined from stale conversation
- Add a `/dreams` dashboard page (Journal / Resolutions / Plans / Controls) with the draft review queue, approvals, announcement log, and run-now control
- Add session-bound plan tools so participants adjust plans conversationally — "we sorted it" completes a plan, a progress note marks it actioned, and links enforce that only the originating session's participants can touch a plan
- Add a mechanical monologue guard to realtime calls: after an over-long assistant turn the bridge re-tightens session instructions (capped at two nudges)
- Add a trusted-only `create_contact` agent tool so agents can dial numbers not yet in the directory; agent-created contacts are outbound-only by default

### Changed
- Soften the outbound call opening: the voice agent no longer leads with "I'm an AI calling on Mike's behalf" — whose behalf it calls on, and its AI status, is revealed only when the goal calls for it or the person asks, and answered honestly when asked
- Cap the voice agent's turns at a sentence or two with one question at a time, working the goal through as a dialogue instead of reciting it as a monologue
- Reframe voice-call goals as private notes with a concrete customer-style opening, a hold-music silence rule, and no self-introduction on transactional calls — scripted goals were being recited verbatim, overriding the phone-manner rules, so the goal contract now demands factual briefs instead of staging
- Split contact existence from inbound DM permission (`allow_inbound_dm`): being in the contact list no longer lets a number open a WhatsApp DM session; existing contacts keep inbound DMs while agent-created ones are outbound-only, with a chip, filter, and toggle in the contacts UI

### Fixed
- Fix the dashboard hang-up button silently failing: the call page posted to a dashboard route that didn't exist (only the Twilio-webhook mount had one) — add the authenticated endpoint and surface the error in the UI
- Fix dream dedup matching nothing: the embeddings table silently used sqlite-vec's default L2 distance while the threshold was calibrated for cosine, so re-observation created duplicates instead of merging — recreated with `distance_metric=cosine` and re-embedded via `bob dream reindex`

### Removed
- Remove the `/approve` WhatsApp slash command; approving unknown numbers for DMs is now a dashboard decision

## 2026-08-15

### Added
- Add a unified voice dispatch service as the single owner of realtime call placement — canonical outbound and inbound instruction builders, a shared modality alias table, contact-call dispatch, hang-up, and completion helpers — so routers and tools no longer import call placement directly
- Add durable dispatch metadata for realtime calls (instructions, voice, subagent id, structured outcome, dispatch timestamp) so a server restart between dial and answer no longer loses the call, results cannot be double-dispatched, and killing a voice subagent hangs up its live phone call or expires its voice-link
- Add `openai_voice` subagent dispatch: `create_subagent` with a contact and modality ("phone" or "voice_link") places the call synchronously, tolerates alias agent-type and modality vocabulary the LLM invents, and echoes back a coerced modality so the LLM can self-correct
- Add structured call outcomes: report-success/report-failure results are stored as JSON on the call or session record and fed to the summariser, replacing tool output pasted into the transcript as prose
- Add live turn-by-turn transcripts and structured outcomes to the dashboard call detail page, with the legacy exchange view retained for pre-realtime history
- Mirror browser voice-link calls into the calls UI with full lifecycle sync so both call modalities appear alongside Twilio calls
- Persist partial transcripts after every turn boundary so call progress is visible mid-call and survives a bridge crash

### Changed
- Route all phone calls, inbound and outbound, through the realtime bridge, with inbound-specific instructions for people calling Bob's number
- Adopt callee-speaks-first on outbound calls and browser voice-link sessions, with server VAD driving the agent's first turn and a fallback nudge to open after 8 seconds of silence on a dead line
- Normalise the bare modality "voice" to phone so explicit phone-call requests place real calls instead of browser links
- Expire untapped voice-link invites after 24 hours instead of leaving them ringing indefinitely
- Rewrite the README architecture diagram and phone/voice documentation for the realtime bridge, including the realtime environment variables, the realtime modules in agent-facing repo instructions, and read-only markers on the legacy per-exchange tables

### Fixed
- Fix inbound phone calls, which never reached the realtime bridge and were dead on arrival
- Fix the agent's voice being garbled on every call by replacing naive decimation with an anti-aliased lowpass downsampler, stopping full-band Realtime output from aliasing into the speech band
- Fix very quiet inbound phone audio by applying +12dB of clip-protected gain to audio that had been arriving at roughly -38dB RMS
- Fix time alignment in call recordings by laying both channels down at their wall-clock send and receive times instead of packing bursty audio deltas sequentially
- Fix barge-in silencing the rest of the call: the outbound audio loop now drops only the interrupted utterance instead of dying on the first interruption
- Fix `end_call` leaving the Twilio leg open and billing dead air: ending the Realtime session now hangs up the phone call so finalisation runs within seconds
- Fix the dashboard hang-up button, which issued a GET against a POST endpoint and never worked
- Fix timestamp parsing in the calls UI to handle both SQLite and ISO formats
- Fix hallucinated openings caused by connection noise, such as greeting the callee by a name invented from a noise burst: the outbound preamble forbids inventing names and treats silence, noise, and unintelligible audio as not-a-greeting, asking for a repeat rather than fabricating content
- Add a 5-second opening gate in callee-first mode that cancels and fully suppresses agent responses created without a completed user transcription, so the agent no longer talks over the caller's hello while real greetings pass untouched
- Fix periodic ticking in the agent's voice on Twilio calls by pacing outbound audio frames on an absolute clock with a 100ms send lead so Twilio's jitter buffer no longer underruns
- Fix realtime calls silently running with no instructions, no tools, and the wrong voice when an unsupported voice was configured: the voice is now validated at config load against the Realtime voice set, falling back to cedar with a warning

### Removed
- Remove the `reach_out_with_voice_call` outreach tool, whose phone branch duplicated the subagent path and whose voice-link default biased the LLM away from requested phone calls; contact reach-out now goes through `openai_voice` subagent dispatch

## 2026-08-11

### Added
- Add an OpenAI Realtime API voice bridge streaming bidirectional audio between caller and the Realtime API, with barge-in handling, turn transcripts, in-call tool dispatch, and time-aligned stereo call recordings — one audio-source-agnostic core serves both Twilio phone calls and browser sessions, configured via new `BOB_OPENAI_REALTIME_*` settings (model, voice, max call duration, turn detection)
- Add the realtime engine for outbound Twilio calls as an alternative to the local STT→TTS pipeline, with voice-agent instructions, voice selection, duration caps, and a curated in-call tool set (end the call, look up the called contact, report success or failure of the task)
- Add a browser test harness for realtime voice so prompts, voices, and tools can be iterated over the exact bridge code path used for phone calls without spending call minutes
- Add browser voice-link sessions: Bob can offer a voice call from a WhatsApp DM by sending a tappable link that shares persona and recent chat context, and on hang-up the transcript is summarised and relayed back to the chat
- Add task-oriented voice reach-out: a voice session carries a goal the voice agent works toward, reporting success or failure as a structured outcome dispatched back to both the contact's chat and the chat that requested the reach-out
- Add a `place-realtime-call` tool so the LLM can dial a contact with custom voice-agent instructions, tracked as a subagent whose result flows back through the usual channels
- Add a heartbeat-driven memory reconciliation task that replaces the trigger that died with the dream pipeline: throttled to once per hour, it selects entities touched in the last 24 hours, applies per-entity `min_interval_hours` backoff, caps at 50 entities per run, and is gated by `BOB_RECON_DAILY_BATCH_ENABLED`

### Changed
- Make silent extraction the only memory path: when a session goes idle (or the agent calls the `remember` tool), an agent-driven extraction turn runs over the session's new messages and writes claims directly to the entity store with per-message provenance (`source_messages`), replacing the transcript→bulletin→dream→claims pipeline end to end
- Show message-count provenance instead of bulletin IDs in reconciliation renders, and dedupe identical claims by merging message provenance
- Hide the raw phone-call tools in WhatsApp DM chats so Bob routes voice requests through the unified voice outreach tools instead of defaulting to placing Twilio calls
- Extend call-result summarisation to realtime-engine calls by summarising the bridge transcript instead of per-exchange records

### Removed
- Remove the dream pipeline and bulletin system entirely: MemoryService bulletin/dream methods, the bulletin generator, and the session/email/manual history seeders
- Remove the memory CLI `seed`, `seed-email`, `seed-manual`, `rebuild`, and `supplement` commands
- Remove the `note` and `memory_write` agent tools, the `search_bulletins` session tool, and the WhatsApp `/bulletin` slash command; memory capture is now exclusively the `remember` tool plus automatic idle extraction
- Remove the `BOB_MEMORY_EXTRACTION_MODE` setting and `MemoryExtractionSettings` (silent is the only mode)
- Remove dashboard bulletin surfaces: `/api/memory/bulletins*`, `/dreams`, `/digested`, and `/redigest` endpoints, `pending_bulletins`/`last_dream`/`bulletin_count` fields, and the dashboard UI's dream log, bulletin cards, and bulletin detail page
- Drop the `memory_bulletins`, `memory_bulletin_entities`, `memory_entity_bulletins`, `memory_claim_bulletins`, and `memory_dream_log` tables plus the `source_bulletins` column on `memory_claims` via migration 353
- Delete the dream eval cases and bulletin-only tests

## 2026-08-08

### Added
- Add a `reasoning_effort` passthrough on LLM dispatch and the OpenAI service, applied as `reasoning.effort` for reasoning-style models
- Add `add_claim`, `replace_claim`, `rename_entity`, and `create_entity` actions to the `memory_correct` tool, letting the agent attach or replace individual claims, rename mislabeled entities (rewriting claim/relation references), and materialize missing typed entities
- Refresh entity FTS and embedding indexes immediately after every memory correction so natural-language recall sees corrections right away instead of waiting for the next background cycle
- Validate that the subject entity exists before `set_truth`/`add_claim`/`replace_claim` instead of silently creating orphan claims
- Allow `truth` claims on daylog entities and add GPT-5.6 family pricing to the dashboard cost breakdown

### Changed
- Bump default models to the GPT-5.6 family: OpenAI, harness, and local-subagent defaults move to `gpt-5.6-sol`, the patience gate moves to `gpt-5.6-luna`, and GPT-5.6 models are treated as reasoning models that skip the temperature parameter
- Run the patience urgency check at low reasoning effort with a 300-token budget so the gate can complete its reasoning
- Drop "truth" rows from rendered entity templates (person, attraction, stay, connection) so user corrections no longer duplicate the facts they override

### Fixed
- Fix the WhatsApp bridge `/health` endpoint reporting `whatsapp_connected` based on whether a bob-server client was attached; it now reports the actual WhatsApp websocket status and a separate `client_connected` field

## 2026-07-26

### Added
- Recover Hermes-style `<tool_call>` XML that GPT-5.x models sometimes emit as plain text instead of native function calls: the OpenAI service parses these blocks, dispatches the named tool handlers, strips the XML and trailing "Done." residue from the user-visible reply, and logs what was recovered, in both streaming and non-streaming paths
- Normalize hallucinated `functions.`-prefixed tool names (e.g. `functions.send_whatsapp_message`) when recovering XML tool calls
- Support streaming and downloading `.mkv` files from the dashboard workspace file viewer

### Fixed
- Strip `<tool_call>` XML from message content when serializing model output for session history so recovered calls cannot poison future turns via replay
- Record only actually-delivered reply text into WhatsApp session history instead of the raw LLM output, preventing `<tool_call>` XML and other unprompted model output from leaking into replayed conversation context

## 2026-06-30

### Added
- Add a daylog entity type as the retrospective counterpart to dayplan, with date/notes/media_ref/attraction claims, a render template, and trip→daylog references
- Add a Home Assistant location integration: a pull-based `current_location()` tool backed by the HA REST API, a `location_history()` tool, and a scheduled background task that records GPS pings every 15 minutes into a location-history table
- Add a memory feed of recent claims to the home dashboard, with per-claim-type badges and deep-links into the memory page

### Changed
- Strengthen dayplan extraction so forward-looking plans ("tomorrow we…", "we've booked…") always create a dayplan entity, and route past-day activity to daylog instead of event or trip notes
- Guard claim writes against orphan subjects: claims referencing a non-existent entity are rejected, and the extraction tool surfaces the error inline so the LLM can create the entity and retry within the same turn
- Restructure trip/stay/connection/daylog entity render templates with markdown section headers, adding Day Plans and Day Logs sections to trip renders
- Expand the `find()` tool description and memory prompt guidance with the full entity-type list and explicit rules to consult memory before answering schedule or history questions

### Removed
- Remove the recent-bulletins widget from the home dashboard, superseded by the claims-based memory feed (bulletin totals remain in the stats box)

## 2026-06-26

### Added
- Add optional relevance gating to patience: the patience LLM also decides whether Bob should respond at all, and batches judged not addressed to Bob are marked dispatched without invoking the main LLM; toggleable per session via a new `/relevance` slash command
- Add download support (Content-Disposition attachment) to the workspace file endpoint and a download link in the dashboard workspace viewer

### Changed
- Route all WhatsApp messages through the patience buffer regardless of mode: with patience off, messages now batch behind a fixed settle delay (default 1.5s) instead of dispatching immediately
- Track patience evaluation state so already-evaluated messages drop out of future patience contexts, and make the buffer safety cap flush without dispatching after a skip decision rather than force a main-LLM call
- Include the stored session agenda in the patience LLM context so it can distinguish messages addressed to Bob from messages merely mentioning third parties
- Inject a local-time header into routine prompts at dispatch so routine output anchors to the routine's timezone instead of the model's UTC sense of "today"
- Withhold routine management tools from a routine's own dispatch so it executes the action instead of drifting into editing routines

### Fixed
- Fix routine due-ness comparisons across timezones by normalizing both sides with datetime(), stopping continuous re-firing of offset-mismatched routines (e.g. a Europe/Paris routine under an Australia/Perth server clock)
- Restore undelivered session messages when the LLM call fails on OpenAI quota exhaustion so they retry once credit returns, and notify the chat once per hour instead of silently swallowing the batch

## 2026-06-22

### Added
- Add per-routine IANA timezone and valid_from/valid_until validity windows to routines, honored by the cron scheduler, routine tools, and next-occurrence calculation, with validation of timezone names and bound formats
- Add verbose memory notices: silent extraction turns post a system notice listing new entities and claims, published as an event and forwarded to the WhatsApp chat when enabled per session
- Add `/verbose on|off|status` and `/silentmem` slash commands to toggle the notices and to trigger an immediate silent extraction turn on demand
- Add WhatsApp document (file attachment) support: the bridge downloads document messages and copies them into workspace/whatsapp_media, surfacing the saved path to the agent for inspection

### Changed
- Harden claim writes with pre-write validation that rejects malformed file_ref claims and resolves object_id/value collisions, and make extraction/reconciliation tools tolerate non-array claims_json and skip invalid claims instead of failing the batch
- Sharpen extraction and reconciliation prompts with per-type miscategorization rules (preference, truth, milestone) and narrow the milestone claim type to qualitative lifecycle events, explicitly excluding changelog entries, release notes, and bug-fix summaries

## 2026-06-20

### Added
- Add a silent-turn memory extraction mode (`BOB_MEMORY_EXTRACTION_MODE=silent`): an idle-triggered agent tool-loop on the memory model records claims directly via claim-creation tools, attributing provenance to the turn's session message instead of a bulletin; the bulletin pipeline remains the default
- Add a `remember` tool that, in silent mode, flags a conversation for immediate extraction right after the current reply completes, with an optional hint steering the extractor
- Add message-level claim provenance (`source_messages`) and an extraction-turn tracking table, seeded for existing sessions so enabling silent mode doesn't trigger a one-time surge over historical conversations
- Teach the memory extraction prompt to capture person-level claims (durable preferences, interests, jobs, pets, traits) with few-shot examples, so casual mentions are stored as person facts rather than only relationship-bob claims
- Persist each dispatch's tool-call trace on the LLM call log and surface it in the dashboard call detail view, so tool calls shown are the dispatch's own instead of replayed items parsed from chat history
- Sync contact renames to the linked person entity's display-name snapshot across the contacts API, dashboard, WhatsApp contact sync, and `/approve`, and retire the contact_id claim when a contact is soft-deleted so the link doesn't dangle
- Add a copy-entity-id button to the memory entity detail header in the dashboard

### Changed
- Restrict the memory supplement stage to compositional entity types (trip, stay), skipping atomic types like person/group/connection where the cross-entity bulletin walk misattributed facts onto the wrong entity
- Resolve person entities by contact_id claim before falling back to name slug, preventing duplicate person entities when a contact is renamed
- Update the LLM pricing table and bill cached input at 10% of the input rate instead of 50% in dashboard cost estimates
- Strip OpenAI web_search citation markers from outgoing WhatsApp messages, since [N] markers can't be rendered without a reference map
- Hide the note/memory_write bulletin-authoring tools when silent extraction is on, replacing them with the `remember` tool
- Rewrite the stale memory prompt section to advertise the real tool set (recall/find plus the mode-appropriate capture tool) instead of non-existent memory_search/memory_read/memory_browse/memory_graph tools

### Fixed
- Fix heartbeat idle detection and memory windowing queries by comparing created_at columns through datetime(), so mixed-format timestamps no longer break window boundaries
- Fix `bob serve` to honor env-backed settings via dataclasses.replace instead of a hand-copied field allowlist that silently dropped memory_extraction, reconciliation, and patience configuration

### Removed
- Remove the redundant `memory_read` tool, folding its behavior into `recall` which already covers it

## 2026-06-18

### Added
- Add `self` and `relationship` memory entity types with 14 new claim types (migration 342), giving Bob a self-model and per-person relationship records populated through the existing dream/extraction pipeline
- Add `/approve <phone> [name]` slash command letting trusted contacts pre-authorize unknown numbers so their WhatsApp DMs are not dropped by the contact-existence gate
- Add WhatsApp bridge HTTP `/upload` endpoint with single-use in-memory UploadStore (5m TTL, 100 MiB cap) so large media like PDFs can bypass the ~770KB WebSocket frame ceiling via `upload_id`
- Add shared harness venv at `~/bobenv`, auto-created on startup, that the bash tool activates so `python`/`pip` resolve consistently across skills (replaces per-skill pyproject.toml)
- Add WhatsApp bridge lifecycle CLI (`bob whatsapp service install|uninstall|start|stop|restart|status|logs`): builds the Go binary, writes the systemd unit, probes the bridge over TCP for status
- Add unified bridge auth via `BOB_WHATSAPP_BRIDGE_TOKEN` (replaces `WHATSAPPBRIDGE_TOKEN`) read from the shared config dir by both processes, plus shared `.env` loading with first-definition-wins precedence mirroring the Python service
- Add daily-rotating file logs: Python writes to `~/logs/{YYYY-MM-DD}_bob-server.log`, Go bridge to `~/logs/{YYYY-MM-DD}_whatsappbridge.log`, both archiving prior days to `~/logs/older/` at midnight and on startup
- Persist tool-call traces (`tool_summary`, `tool_blocks_json`) on assistant session_messages via migration 343; last 3 assistant rows expand inline during prompt assembly, older rows fall back to bracketed summaries
- Add per-iteration LLM usage accumulation across tool-call rounds in `chat_with_tools` and `chat_stream_with_tools` (previously dropped intermediate tokens, undercounting by 30-60%)
- Add claim-type glossary to memory reconciliation so the LLM can detect and retract claims whose values violate their type definition
- Add `docs/datamodel.md` reference with 6 Mermaid ERDs (sessions/messaging, dispatches, phone calls, email, calendars/events, notifications/webhooks)

### Changed
- Refactor three oversized modules (`cli.py` 2516 lines, `dashboard_api.py` 2066 lines, `whatsapp_bridge_service.py` 1691 lines) into per-domain package layouts; public APIs unchanged, the WhatsApp service now composes `GroupEventsMixin` + `SlashCommandsMixin` via MRO
- Rename dashboard cost-table columns from `prompt`/`completion` to `input`/`output` to match OpenAI API terminology
- Move data-model section out of README into `docs/datamodel.md`, registered in `DOCS.yaml`
- Finish Cyborg→Bob rename in README and AGENTS.md (clone URL, env defaults, paths) and replace stale Jinja2/dashboard description with the actual React SPA at `ui_app/`/`ui_dist/`

### Fixed
- Fix file logging going silent after startup: `serve()` now carries `log_path`/`log_dir`/`debug` into the explicit `Settings`, and uvicorn runs via a `_PreserveLoggingConfig` subclass whose `configure_logging()` is a no-op so it cannot clobber root handlers mid-runtime
- Fix routine scheduler double-fire race by advancing `next_run_at` atomically in `claim()` before dispatch
- Add memory addressing guard so `self-bob` and `relationship-bob-*` claims are only written when Bob is actually being addressed (not during human-to-human conversation or when silent)
- Fix two un-awaited `ensure_person_entry` async calls and the stale `_wa_service` reference in the WhatsApp bridge email polling path
- Remove eager module-level `create_app()` call at import in `main.py`
- Fix broken `pytest` collection at HEAD by deleting stale tests referencing removed `DispatchStatus`/`BlockedProjectCheckTask` symbols

### Security
- Block WhatsApp DMs from numbers with no contact row: previously any unknown sender was auto-seeded as an untrusted contact and dispatched; now their messages are logged as warnings and dropped before session creation. Group members remain unaffected since group sync auto-seeds contacts.

### Removed
- Drop `DatabaseLogHandler` and the `structured_logs` table: the handler's asyncio task was never tracked, writes were silently dropped, and nothing queried the table (migration 341 drops it)
- Remove committed artifacts (`data/cyborg.db`, `packages/bob-server/cyborg.db`, three `cyborg-*.png` branding assets), dead `memory_service.py`/`memory_service_v1.py` (zero references), and the stale `persona-plan.md`

## 2026-06-13

### Added
- Add persona configuration system with versioned DB records: SOUL, IDENTITY, AGENTS, and USER sections are stored as revision-tracked records editable through a new dashboard page, with framing headers hardcoded so users cannot accidentally modify them
- Add raw-transcript bulletin format replacing LLM-summarized bulletins: each bulletin captures the actual session messages with name/contact_id/timestamp labels, plus N prior context messages marked "do not extract", eliminating information loss from the summary stage
- Add synthetic flag on assistant messages whose dispatch used memory-read tools (recall, find, memory_read), keyed per-dispatch_id so concurrent dispatches cannot cross-pollute; extraction prompts skip these lines to prevent recalled facts being re-ingested as new ground truth
- Add per-entity and per-entity-type model overrides for memory reconciliation: BOB_RECON_LARGE_MODEL_TYPES env var routes specific entity types to the large model, and the recon_model_overrides table plus `memory model-override-set/remove/list` CLI commands allow pinning specific entities
- Add web_search citation rendering: OpenAI Responses API citation placeholders are now replaced with `[N]` markers in text plus a Sources list of bare URLs (WhatsApp renders bare URLs as clickable)
- Add `bash` workspace tool replacing the previous ls/read/write/grep/glob/run_script toolset with a single flexible shell command tool (30k char output truncation, 900s timeout)

### Changed
- Migrate default runtime paths from `~/.config/cyborg`, `~/.openclaw` to `~/config`, `~/data`, `~/workspace` for a cleaner single-vendor layout
- Move Memory into primary dashboard navigation and surface entity/bulletin counts on the home page
- Add structured logging for incoming, outgoing, queued, and drained WhatsApp bridge messages with content previews
- Drop persona-file references from skill developer prompts now that persona is DB-backed

### Removed
- Remove file-specific workspace tools (ls, read, write, grep, glob, run_script), replaced by the single bash tool

## 2026-06-12

### Changed
- Rename Cyborg to Bob across the entire codebase: package names (cyborg-server→bob-server, cyborg-core→bob-core, cyborg-cli→bob-cli), import paths, environment variables, configuration references, and documentation

## 2026-06-10

### Added
- Add local subagent execution mode: subagents can now run in-process via LLMDispatchService (default model gpt-5.5) instead of spawning external Claude CLI processes, with optional persona loading for full agent context
- Add `cp` workspace tool for copying files; extend `mv` to accept source paths outside the workspace (e.g. incoming email attachments)
- Add attraction and dayplan entity types with dedicated schemas and rendering templates
- Add connection, stay, and trip entity templates for richer memory entity rendering

### Changed
- Update bulletin generation prompt to ignore assistant recall/reiteration, preventing memory echo from assistant messages that simply repeat existing memory
- Expand email tools and delivery service
- Revise routine scheduler and tools
- Include full harness workspace directory in backup script

## 2026-06-09

### Added
- Add tool-based reconciliation loop replacing the previous JSON-operations approach: the LLM now has `get_entity`, `list_entities`, `add_claim`, `retract_claim`, `supersede_claim`, `create_entity`, `delete_entity`, and `merge_entities` tools to inspect and fix entities directly
- Add orphan connection linking rules to reconciliation: connections without a parent trip are automatically discovered and linked by the LLM using entity read tools
- Add `gpt-5.5` pricing to the dashboard cost tracker

### Changed
- Switch reconciliation from `memory_model` (small model) to the default bigger model for more capable autonomous entity repair
- Change connection entity extraction from one-entity-per-booking to one-entity-per-hop: multi-leg journeys under a single PNR are now separate connection entities with shared `booking_ref`
- Add reconciliation rule for stays to enforce exactly one arrival and one departure date, retracting duplicates

### Removed
- Remove inline `_apply_operations()` reconciliation dispatcher, replaced by tool-based LLM loop

## 2026-06-08

### Added
- Add routines system: cron-scheduled prompts injected into sessions via `read_routine`/`write_routine`/`delete_routine` agent tools, with a `RoutineSchedulerTask` heartbeat task that fires due routines independently without blocking session activity
- Add entity merge system for detecting and merging duplicate entities using embedding cosine similarity and LLM confirmation, with a CLI command (`bob memory merge --dry-run`), a dashboard API endpoint, and an inline merge UI in the memory dashboard
- Add centralized entity type registry (`ENTITY_TYPE_REGISTRY` in `claim_types.py`) consolidating per-type metadata (prefixes, descriptions, keywords, extraction rules, reconciliation rules, display behavior)
- Add `connection` as a first-class entity type replacing the old `transport` type, with structured claim types for departure/arrival locations, times, transport type, duration, booking ref, route, passengers, and seat
- Add `stay` entity type replacing `tripstop`, with renamed claim keys (`accommodation`, `arrival_date`, `departure_date`, `accommodation_type`, `accommodation_address`)
- Add new claim types: `preference` (person), `interest`/`opening_hours` (location), `attraction` (trip), `booking_ref`/`route`/`passenger`/`seat` (connection)
- Add `find_session` and `search_bulletins` agent tools for discovering sessions by name and searching memory bulletins by time horizon with trust-scoped access control
- Add `list_attachments` and `download_attachment` email tools for browsing and saving email attachments, with attachment metadata persisted to the database for all messages
- Add Jinja2-based entity template engine for rich entity rendering with recursive entity reference resolution
- Add entity deprecation status (`active`/`archived`/`deprecated`) and automatic deprecation of file entities with no valid `file_path` claim
- Add `bob memory reindex` CLI command for rebuilding the FTS search index without LLM calls
- Add PDF file viewer in the workspace dashboard using an embedded iframe
- Add `deprecated` entity status to the database schema and migrations (schemas 325–328)

### Changed
- Convert entity rendering to async across all call sites to support recursive entity reference resolution via database lookups
- Improve supplement prompt to prevent cross-entity claim extraction: claims must be about the target entity, not about other entities mentioned in the bulletins
- Persist source bulletin IDs on supplement-generated claims instead of leaving them empty
- Skip all entity-ref claims during supplement to prevent inferred relationships
- Remove self-referential claims created during entity merges
- Change entity ID display in memory dashboard from CSS-truncated to word-wrapped full IDs
- Switch datetime handling in routines and cron to use local timezone instead of UTC
- Handle `CancelledError` gracefully in LLM dispatch: catch `BaseException` and log cancellations as "server restart" instead of generic errors
- Store attachment metadata for all email messages on receipt, then auto-download for trusted senders
- Merge actual email reply body text into assistant session messages (matching WhatsApp behavior)
- Move email attachment downloads from `projects_base_dir` to `data_dir`

### Fixed
- Fix supplement producing text-value connection claims instead of entity references by skipping all entity-ref claim types during supplement extraction
- Fix embedding upsert to use DELETE+INSERT instead of INSERT OR REPLACE to handle stale data in sqlite-vec

### Removed
- Remove orphan transport discovery (`_find_orphan_transports`) from reconciliation, replaced by per-connection orphan linking rules

## 2026-06-07

### Added
- Add `POST /api/v1/email/poll` endpoint for on-demand email inbox polling with `force=True` to bypass the interval check
- Add `/email` slash command skill for triggering immediate email inbox check
- Add entity reconciliation system with LLM-driven consistency checking, per-type rules, and human-in-the-loop conflict resolution via questions
- Add `supplement_entity` pipeline to gap-fill missing claims from related bulletins after dream processing
- Add `memory_correct` tool supporting `remove_entity`, `remove_claim`, and `set_truth` actions for agent-driven memory correction
- Add `truth` claim type for user-stated facts and corrections that override inference, with migration of legacy `purpose` answer claims
- Add `memory reconcile` and `memory supplement` CLI commands for manual entity consistency repair and gap-filling
- Add memory questions API endpoints and QA tab in dashboard for surfacing and resolving open reconciliation questions
- Add stats tab to memory dashboard showing entity distribution, pipeline status, and claim counts
- Add `rm`, `mv`, and `find` workspace tools for file deletion, move/rename, and content search
- Add `LLMCallStalenessTask` heartbeat to detect and mark LLM calls stuck in "running" status after 30 minutes
- Add orphan claims rendering in entity detail display for claims not covered by the entity template
- Add tool args and output display to live tool call cards in session call detail view
- Add `on_iteration_complete` callback to OpenAI tool-call loops for persisting intermediate messages to LLM call logs

### Changed
- Trigger memory dream immediately on bulletin write with 2-second debounce instead of waiting for the heartbeat cycle
- Run supplement and reconciliation automatically after each dream cycle for all touched entities
- Add pagination to email inbox polling for handling >50 unread messages
- Replace `list_files` workspace tool with `ls` (non-recursive, single-directory listing)
- Replace memory "lint" button with the new QA questions workflow
- Increase claim extraction `max_tokens` from 2000 to 4000
- Simplify trip entity model: remove destination/date claims at trip level, rely on tripstop-level data instead
- Strengthen transport entity extraction rules: require transport entities for all flights/trains/buses with route details
- Add explicit timeouts (300s read, 30s connect) to OpenAI client
- Include tool args and output summary in `llm.call.tool_completed` WebSocket events

### Fixed
- Fix backfilled emails never dispatched: poll now re-dispatches messages that were imported via sync but never reached the LLM
- Fix emails stuck unread in AgentMail: deduplicated messages are now marked as read even when processing is skipped

### Removed
- Remove dream trigger from `SessionIdleSummaryTask` heartbeat (moved to debounced bulletin-write trigger)
- Remove `/api/memory/lint` endpoint and its corresponding UI button

## 2026-06-06

### Added
- Add embedding-based semantic search using OpenAI text-embedding-3-small and sqlite-vec: entities are embedded at write time and queries like "what type of car does david have" now find results via cosine similarity when FTS5 keyword search fails
- Add bulletin detail page at `/memory/bulletins/{id}` showing source session/type, full text, and all claims extracted from that bulletin
- Add person entity rendering on contact detail page with rendered body display and "view in memory" link
- Add claim type registry (`claim_types.py`) with type-specific render templates that generate human-readable entity views from claims
- Add `render_entity()` function that deterministically renders entity claims into structured text using per-type templates (person, group, event, trip, etc.)
- Add FTS5 index built from rendered entity templates instead of raw claim data, improving keyword search relevance
- Add `rebuild_embeddings()` method for batch embedding all entities and `sqlite-vec` extension loading in database connection pool
- Add schema migrations 314–322: FTS5 index, claim types registry, claims v2 (claim_type_key replacing type/predicate/body), entity type renames, template-based FTS, and embedding vectors table
- Add file_path validation during claim extraction: file entities without a file_path claim are dropped automatically

### Changed
- Rename entity types: contact→person (slug-based IDs like `person-mike-cleaver`), artifact→file (requires file_path) and thing (physical objects with thing_type)
- Replace entity body documents with claim-only model: entities have no body column, all content is derived from claims via render templates
- Replace LLM-powered entity update with deterministic claim extraction: claims are the source of truth, entity views are generated on demand
- Add name-slug fallback for contact-to-person entity lookup when contact_id claim is missing
- Pre-map `{{contact:HEX8|Name}}` tags to `{{person-slug|Name}}` before LLM extraction so the LLM never sees raw contact IDs
- Update dashboard search to try embedding similarity when FTS5 returns no results
- Update memory recall tool with hybrid retrieval: exact ID → alias → embedding similarity → FTS5
- Update memory documentation to reflect current architecture

### Removed
- Remove social_relation claim type from registry, templates, and database (LLM over-generated it, producing noise)

## 2026-06-05

### Added
- Add patience system for LLM-driven message batching with WhatsApp typing awareness: buffers incoming messages, subscribes to contact presence, and dispatches only when the user stops typing
- Add ContactDirectory service for loading and querying contacts DB with `as_known_entities()` for bulletin generator context
- Add contact ID reconciliation for mapping non-canonical contact IDs (name slugs, unresolved variants) to canonical UUIDs during entity updates
- Add entity cleanup pipeline: merge duplicate contact entities, build renaming maps, rewrite claims/bulletins/entity relations, and run orchestrated cleanup via CLI
- Add email thread tools (`email_thread_read`, `email_thread_search`) for LLM function calling with trust-scoped access control
- Add email memory seeding from email thread history with seed_email CLI command
- Add phone call result service for surfacing call outcomes back to originating sessions
- Add email thread result tools for surfacing email outcomes back to originating sessions
- Add generic thread result service for linking communication threads to their origin sessions
- Add contact entity and claims API endpoints (dashboard and REST) for viewing memory data per contact
- Add contact detail page in dashboard UI showing entity document and claims
- Add phone call hangup endpoint via Twilio API
- Add phone call status persistence (ringing, active, completed, duration, recording path) to database
- Add schema migrations 307–313: memory tables in SQLite, phone/email origin session links, simplified bulletins, entity-bulletin links, session range tracking, drop session summaries table
- Add SQLite-backed memory storage replacing file-based bulletin/claim/entity persistence
- Add manual bulletin seeding via CLI (`seed_manual`) for ad-hoc memory injection

### Changed
- Simplify bulletin model from structured metadata (entities, scope, session tracking) to plain-text notes with inline `{{contact:ID|Name}}` tags
- Simplify bulletin generator input from verbose structured transcript to compact message list with sender contact IDs and timestamps
- Replace session summary service with bulletin-based idle session processing in heartbeat task
- Replace summary cards with bulletin cards in dashboard home view
- Update memory dashboard route with enhanced entity browsing and claim visualization
- Migrate memory service from file-based YAML storage to SQLite with related entity parsing from body text
- Gate tap dispatch behind `tap_enabled()` check instead of unconditionally running on every non-tool response
- Update WhatsApp bridge to subscribe to contact presence for patience system integration
- Strengthen bulletin generator prompt to forbid inventing IDs for known contacts
- Pass known entities from ContactDirectory to bulletin generator for canonical ID enforcement
- Add email polling integration with patience gate for coordinated message processing
- Update memory CLI with new seed commands and cleanup orchestration

### Fixed
- Fix phone call recording finalization on call completion with proper path persistence
- Fix phone call status transitions to persist all intermediate states (ringing, in-progress) to database

## 2026-05-31

### Added
- Add entity-centric memory system (v3) replacing wiki/category model: channel-based architecture with bulletin generation from session transcripts, entity resolution, claim extraction, and graph-based entity relationships
- Add memory bulletin generator that converts session transcript ranges into structured draft bulletins with channel, visibility, scope, and entity metadata
- Add memory claim service for extracting knowledge claims from bulletins and linking them to entities
- Add memory entity resolver for mapping contact references, channels, and topics to entity IDs
- Add memory index service for building searchable text indexes from entity directories
- Add memory CLI commands: `seed` (regenerate from session history), `rebuild` (rebuild indexes from bulletins), `validate` (check structure), `query` (natural language search)
- Add frontend error reporting: unhandled exceptions and promise rejections are POSTed to a backend endpoint for centralized logging
- Add live tool call tracking in call detail view: WebSocket events show running tools with pulse indicator and output display
- Add `log_id` to `llm.call.running` and `llm.call.tool_completed` WebSocket events for precise call tracking

### Changed
- Replace wiki/category memory tools with entity-centric tools: `memory_search`, `memory_read`, `memory_browse`, `memory_write`, `memory_graph` with entity types (contacts, groups, channels, trips, locations, events, tasks, artifacts, decisions)
- Replace memory_prompts and people_updates in session summaries with direct bulletin generation from transcripts via the heartbeat task
- Simplify session summary LLM prompt to focus on summary and topics only, removing memory extraction responsibilities
- Switch dashboard session call tool count from counting offered tools (`tools_json`) to counting actual executed tool calls (`function_call` entries in `messages_json`)
- Fix active call tracking in session view to remove only the specific completed call by `log_id` instead of clearing all running calls
- Deduplicate live running calls in session timeline so DB-recorded running calls don't appear alongside WebSocket-tracked calls
- Update memory dashboard dream log display to show per-bulletin breakdown with claims and entity ops instead of category/slug pairs
- Simplify memory prompt in workspace assembler to reference new entity-based tools and omit inline wiki documentation

### Fixed
- Fix animated GIF sending via WhatsApp: preserve GIF animation by passing files under the bridge payload limit as-is, and add frame-dropping resize for oversized animated GIFs instead of flattening to static JPEG

## 2026-05-30

### Added
- Add multimodal image support: Go bridge downloads incoming WhatsApp images to disk and forwards metadata to the Python server, which stores image references in message metadata and reconstructs OpenAI `input_image` content parts in the prompt for GPT-5.5 vision
- Add WhatsApp group understanding with member tracking: new `whatsappgroups` and `whatsappgroup_members` tables replace flat text column, Go bridge handles GroupInfo and JoinedGroup events, member changes trigger LLM dispatch in group sessions
- Add group participants tool for LLM function calling in WhatsApp group sessions
- Add SyncGroups on connect to populate group tables from existing WhatsApp memberships (not just new joins)

### Changed
- Add media_dir configuration to WhatsAppBridgeSettings for configurable image storage path
- Improve workspace read_file tool to recognize image extensions and return a descriptive message instead of binary file error

### Fixed
- Fix migration 305: disable foreign key checks during contacts table recreation to prevent constraint failures from whatsappgroup_members references
- Fix contact detail page: update dashboard API and React component to query new group tables instead of removed whatsapp_groups column
- Fix LLM dispatch and OpenAI service logging to handle multimodal message content (list of content parts) without crashing

## 2026-05-28

### Added
- Add subagent system with Claude Code CLI integration, replacing skill-specific delegation with a generic async subagent service that spawns Claude processes and tracks status, cost, and results
- Add subagent tools (create_subagent, message_subagent, kill_subagent) for LLM function calling, with automatic result injection back into parent WhatsApp sessions
- Add subagent lifecycle management: stale subagent cleanup on startup, status tracking, and event-driven result delivery via event bus
- Add session messages to dashboard session detail view, showing conversation entries from session_messages table alongside LLM calls and summaries in the timeline
- Add subagent session classification in dashboard to distinguish subagent sessions from WhatsApp sessions by key prefix
- Add visual distinction for subagent messages in session timeline: amber for task messages (→ subagent) and teal for response messages (subagent →)
- Add diagnostic logging for empty OpenAI responses to capture refusal details, status, and output types
- Add skill-guru skill to guide creation of new workspace skills via subagent delegation

### Changed
- Replace full memory index (~8KB) in system prompt with concise memory tool reference (~400 bytes), reducing per-call token usage by instructing the agent to use memory_search instead of loading all entries
- Add explicit send_whatsapp_message instruction to incoming WhatsApp user prompts to improve tool call reliability with gpt-5.4-mini
- Update subagent result notification to instruct agent to relay results via send_whatsapp_message instead of only referencing message_subagent/kill_subagent
- Update session agenda template to reference subagent tools instead of deprecated skill delegation
- Replace skill delegation tools with subagent tools in the tool registry

### Fixed
- Fix WhatsApp reply delivery failure caused by empty chat_id on DM session routes: store chat_id (WhatsApp JID) alongside contact_id for DM routes so subagent result dispatch can resolve the outbound address
- Relax session_routes CHECK constraint and Pydantic validator to allow DM routes to include chat_id
- Backfill missing chat_id on all existing WhatsApp DM routes from metadata sender_jid
- Fix subagent sessions displaying as "whatsapp" channel in dashboard by checking subagent: prefix before :whatsapp: in channel parser

## 2026-05-24

### Added
- Add centralized tool registry with `build_common_tools()` replacing duplicated tool assembly across WhatsApp and email dispatchers
- Add tap dispatch system: follow-up LLM call when agent doesn't use send tool, replacing auto-send of raw text output
- Add TapCard UI component in dashboard to visually distinguish tap follow-up dispatches from regular messages
- Add dreaming memory system with bulletin pipeline, LLM-driven dream curation, and conflict resolution across entries
- Add reply tracking to WhatsApp and email dispatch to detect whether agent called the send tool

### Changed
- Rewrite all session agenda templates (WhatsApp, email, phone) with prominent DELIVERY sections instructing the agent to use send tools
- Update grounding rules to emphasize text output is invisible and only tool calls have effect
- Convert memory writes to bulletins: both manual `memory_write` and automatic `reflect_and_update` now produce bulletins for dream curation instead of direct category writes
- Trigger memory dream process after each heartbeat summary batch to curate bulletins into proper categories
- Pass session metadata (time window, participants, contact IDs) through to memory reflection
- Update ARCHREVIEW.md tool registry item to reflect centralized tool assembly

## 2026-05-23

### Added
- Add memory wiki subsystem with search, reflection from session summaries, bulk seeding CLI, dashboard search UI, and LLM function-calling tools
- Add docs search service with LLM-powered documentation querying and function-calling tools

## 2026-05-22

### Removed
- Remove unused services and routers, streamline codebase

## 2026-05-21

### Added
- Add reflection service for on-demand LLM reflection on session history
- Add rich text component for dashboard UI rendering
- Add generate-docs skill for rebuilding documentation from DOCS.yaml

### Changed
- Restore phone call subsystem with updated integration

## 2026-05-17

### Added
- Add workspace browser UI with file listing, content viewing, and file editing
- Add contact editing in the dashboard with editable contact fields

### Changed
- Make workspace layout responsive: stacked panels on mobile, side-by-side on desktop
- Use vertical file list on mobile workspace instead of horizontal scroll
- Improve WebSocket reliability for dashboard live updates

### Fixed
- Fix workspace image viewing to use FileResponse instead of read_bytes

## 2026-05-16

### Added
- Add session summaries with idle-triggered generation, topic extraction, and dashboard display

### Changed
- Link session summaries to participants and contacts in the dashboard

## 2026-05-11

### Added
- Add session participants tracking with contact resolution, participant name maps, and dashboard UI
- Add WhatsApp outreach tools for initiating conversations with contacts from the dashboard
- Add Claude Code skill delegation system with skill loader, developer service, and frontmatter-based skill parsing

### Changed
- Add WhatsApp NO_REPLY support and auto-send fallback for message delivery

### Fixed
- Fix outreach tool to record full turn in target DM session history

## 2026-05-10

### Added
- Add email and WhatsApp tools for LLM function calling
- Add workspace context injection into agent sessions

### Changed
- Consolidate LLM dispatch to use OpenAI as the sole provider, removing Z.ai provider support
- Swap default model to gpt-5.4-mini

## 2026-05-09

### Added
- Add custom LLM harness with unified dispatch service, tool calling framework, OpenAI-compatible provider, and eval framework
- Add WhatsApp bridge companion service: Go/whatsmeow bridge with persistent queue, WebSocket protocol, and Python-side integration

### Changed
- Route WhatsApp messages through the new LLM dispatch service instead of the deprecated OpenClaw agent gateway

### Fixed
- Fix eval judge blind spot where responses were not properly scored, and align voice evals with production prompt format

## 2026-05-06

### Added
- Add barge-in support and call initiation for phone calls with warmup pipeline and silence detection
- Add phone call subsystem with Twilio integration: outbound/inbound calls via media stream, mu-law audio codec, call recording, and call dashboard

### Fixed
- Fix phone call warmup and silence detection for Twilio media stream calls
- Add ringing and canceled statuses to phone call state machine

## 2026-05-03

### Added
- Add dispatch tracking system with database schema, service layer, and API endpoints for monitoring agent dispatch lifecycle
- Add heartbeat framework with registerable background tasks, cron expression parser, and shared AppContext
- Add voice chat subsystem with real-time STT/TTS engines, WebSocket transport, and bundled reference voices

### Changed
- Refactor dashboard router from a single 2285-line module into a package of sub-modules
- Refactor service layer to accept AppContext instead of raw Database, standardizing dependency injection

### Fixed
- Resolve stuck dispatches on task tap completion

## 2026-05-02

### Changed
- Improve dispatch system reliability and add contact tools for LLM contact lookup and management

## 2026-05-01

### Added
- Add contact trust system with trusted/untrusted sender classification and collapsible email message views

## 2026-04-30

### Changed
- Harden email prompt guidance: enforce reply-vs-send distinction, add identity verification warnings for untrusted senders
- Enforce email thread agendas and fix attachment downloads

### Fixed
- Fix project dispatch routing for next_action notifications

## 2026-04-29

### Added
- Add email attachment support with per-attachment download control for untrusted senders

## 2026-04-28

### Added
- Add email relay system via AgentMail: polling, sending, replying, and inbox management

### Changed
- Filter WhatsApp notification delivery to only needs_input and project_result types

### Fixed
- Fix AgentMail integration bugs including session agenda seeding

## 2026-04-25

### Fixed
- Fix notification routing for auto-created project tasks that have no delivery route
- Fix task file validation to check file existence on disk before registering

### Changed
- Include full user response in next-action prompt after block approval

## 2026-04-24

### Added
- Add Ed25519 device identity for gateway websocket authentication

### Fixed
- Fix doctor command crash when project_id or approval_id is missing

## 2026-04-23

### Added
- Add openclaw-skill pip package with SKILL.md for installable skill

### Changed
- Restructure monolithic codebase into three pip packages (cyborg-core, cyborg-cli, cyborg-server) with proper pyproject.toml files
- Remove hardcoded project workspace paths across config, services, and CLI

### Removed
- Remove planning and progress documentation files
- Remove cyborg-context npm package
- Remove openclaw-plugin source code, slimming plugin to a thin wrapper

## 2026-04-21

### Added
- Add source project discovery and linking: auto-discover related closed projects and link them as sources via CLI and API

## 2026-04-15

### Added
- Add project blocking with user approval flow: create task_input approvals when projects are blocked, enabling dashboard resume

### Changed
- Improve project unblocking after user approval with anti-re-blocking instructions in reasoning prompt

## 2026-04-13

### Changed
- Improve reasoning tuning and prevent agents from using project delete

## 2026-04-12

### Added
- Add project pause/resume controls with CLI commands, dashboard buttons, and background reasoning resume
- Add project notification muting with CLI commands and per-project mute field

### Changed
- Remove plan text from task assignment prompts to reduce confusion; include input file information instead
- Make notification dispatch non-blocking: fire-and-forget pattern instead of blocking API responses

### Removed
- Remove plan service and /plans router; plan functionality now handled through project specs and reasoning

## 2026-04-08

### Added
- Add structured task input approvals: text and multi-choice input schemas for task blocking, with dashboard approval forms
- Add async next-action decision flow with CLI command and OTP-secured API endpoint

## 2026-04-07

### Changed
- Enforce one task at a time per project to prevent concurrent execution conflicts

## 2026-04-06

### Changed
- Lock spec approvals to dashboard UI only, remove state from project updates

### Fixed
- Fix notification retry timing

## 2026-04-05

### Added
- Add task file tracking with CLI command and API endpoint for registering files produced during task execution
- Add upstream task context in reasoning: build parent task results and output file context, inject into all reasoning prompts
- Add fresh reasoning sessions using unique session keys per reasoning call to prevent cross-contamination

### Changed
- Flip auto_execute default to true: projects now auto-execute by default
- Simplify project creation workflow: spec v1 auto-created, plan and method optional
- Make spec method field optional, allowing aim-only projects

## 2026-04-03

### Changed
- Clean up task and spec approval flow

## 2026-04-01

### Added
- Add prompt history recording with database schema, service, and API integration

### Changed
- Improve task execution and reasoning reliability

## 2026-03-29

### Changed
- Refactor dashboard overview to show real workflow state instead of system metrics

### Removed
- Remove standalone tasks dashboard page (merged into other views)

## 2026-03-26

### Changed
- Improve OpenClaw reasoning service with robust JSON response parsing and increased timeouts
- Add planning CLI commands and API endpoints

## 2026-03-22

### Added
- Add learning service for extracting insights from project outcomes
- Add health monitor service with periodic project health checks and risk assessment
- Add structured logging system with correlation IDs, specialized log helpers, and execution timing decorators
- Add database-backed log storage with 30-day retention cleanup trigger
- Add cyberpunk-themed web dashboard with overview, projects, approvals, logs, and health pages
- Add dashboard API with chart endpoints for project status, task breakdown, and health distribution
- Add approvals workflow with database schema and pending/review UI
- Add default contact configuration for unrouted notification delivery
- Add plan approval notifications with context and agent delivery support
- Add correlation ID middleware for HTTP request tracing

### Changed
- Replace mock dashboard data with real database queries across overview, logs, approvals, and project pages
- Simplify DatabaseLogHandler by switching from background thread to synchronous SQLite writes
- Refactor structured logging module and clean up handler implementation
- Improve OpenClaw reasoning service with robust JSON response parsing and increased timeouts

### Fixed
- Fix parsing of success_criteria JSON field in project detail dashboard template
- Fix logs page to query actual structured_logs table instead of using hardcoded mock data
- Fix WhatsApp DM session key format to match expected format
- Fix DatabaseLogHandler global variable initialization

### Removed
- Remove old OpenClaw SKILL.md (replaced by native Context Engine plugin)

## 2026-03-21

### Added
- Add comprehensive CLI with full CRUD commands for tasks, projects, contacts, notifications, session routes, webhooks, events, and OpenClaw integration
- Add OpenClaw reasoning service with plan generation, task evaluation, strategy refinement, and health analysis
- Add context builder service for assembling project/task context for OpenClaw prompts
- Add test suites for OpenClaw acceptance testing and project execution

## 2026-03-10

### Added
- Add FastAPI-based Cyborg data service with SQLite backend and comprehensive CLI
- Add project autonomy service with self-executing projects and plan management
- Add project execution service for automated task orchestration
- Add notification delivery system with channel routing, session route registry, and webhook processing
- Add OpenClaw integration with hook-based gateway communication
- Add test suites for API endpoints, CLI commands, project execution, and webhooks
