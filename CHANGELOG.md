# Changelog

All notable changes to Cyborg are documented here. Entries are based on analysis of actual code changes, not just commit messages.

## [Unreleased] - 2026-05-28

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

## [Unreleased] - 2026-05-24

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
