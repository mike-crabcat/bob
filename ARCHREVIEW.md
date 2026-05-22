# Architecture Review — Cyborg Server

**Reviewed:** 2026-05-21
**Scope:** `packages/cyborg-server/cyborg_server/` — services, routers, data model, config

---

## 1. Stale Branding: "OpenClaw" Everywhere

The gateway formerly known as OpenClaw (port 18789) no longer exists. All LLM calls go directly to OpenAI via `LLMDispatchService`. Despite this, the name persists in ~180 references across 25+ files:

- `OpenClawReasoningService`, `OpenClawHookService` — entire services still named after a dead gateway
- `routers/openclaw.py` — an active router serving `/openclaw/context.*`
- `config.py` — settings classes still reference "openclaw"
- `notification_service.py` — 28+ references, the notification pipeline is conceptually tied to a gateway name that no longer exists
- `prompt_history.py` — audit log helpers branded for OpenClaw
- UI: `phone/$callId/index.tsx` still displays `openclaw_ms`

**Impact:** Every new developer (or future you) has to mentally translate "OpenClaw" to "the LLM dispatch system." It obscures what the code actually does.

**Recommendation:** Rename in order of impact:
1. `OpenClawReasoningService` → `AgentReasoningService` or `LLMReasoningService`
2. `OpenClawHookService` → `AgentHookService` or `LLMHookService`
3. `routers/openclaw.py` → `routers/context.py` (or merge into `context.py` which already exists)
4. Config settings: `openclaw_*` → `agent_*` or `llm_*`
5. Notification pipeline: decouple naming from specific gateway
6. UI timing labels: already renamed in the API (`llm_total_ms`), update the frontend component

---

## 2. ~~Dual Message Storage~~ (RESOLVED)

~~Two independent message storage systems exist~~ The voice system already wrote to `session_messages` via `VoiceSessionStore` delegating to `SessionService`. The `VoiceSessionStore` wrapper has been replaced with direct `SessionService` calls and `LessonProgressService` for lesson-specific logic. Legacy `bobvoice:` keys migrated to `agent:main:voice:session:` format.

---

## 3. ~~Session Key Format Divergence~~ (RESOLVED)

Voice session keys now use `agent:main:voice:session:{user_id}:{mode}` format, consistent with the standard pattern. Dashboard `_parse_channel()` updated to recognize `:voice:` prefix. Historical `bobvoice:` keys migrated in DB via migration 298.

---

## 4. No Repository Layer — Raw SQL Everywhere

Every service inherits `BaseService` which provides `self.db` (a raw database wrapper). Services write SQL directly — `INSERT`, `SELECT`, `UPDATE` statements are scattered across 45+ service files with no centralized data access.

```python
# Typical pattern — repeated in dozens of services
row = await self.db.fetch_one(
    "SELECT agenda FROM session_agendas WHERE session_key = ?",
    (session_key,),
)
```

**Impact:**
- No guarantee that SQL for the same entity is consistent across services
- Schema changes require grep-and-replace across the entire codebase
- No type safety on query results (dict access, not typed models)
- The column rename from `openclaw_ms` → `llm_total_ms` touched 6 files for one rename — this will happen again

**Recommendation:** Introduce repository classes for core entities (sessions, contacts, dispatches, tasks). Start with `SessionRepository` since it's the most accessed from different services. Each repository owns all SQL for its entity and returns typed dataclasses or Pydantic models.

---

## 5. session_routes CHECK Constraint Bug

The `session_routes` table has a `CHECK (channel IN ('whatsapp', 'email'))` constraint at the DB level. The application code now allows `"phone"` as a channel (added in `models.py`), but inserting a phone session route will **silently fail** due to the constraint.

```sql
-- schemas/250_email_relay.sql
channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'email')),
```

**Impact:** Inbound phone calls that try to create a session route will get a constraint violation. This is a production bug.

**Recommendation:** Add a migration that rebuilds the table with `CHECK (channel IN ('whatsapp', 'email', 'phone'))`. Follow the existing pattern from migration 250 which did the same thing to add `'email'`.

---

## 6. Service Coupling and Import Chains

Services frequently import and instantiate each other inline:

```python
# voice_service.py — inline imports during coroutine execution
from cyborg_server.services.dispatch_service import DispatchService
from cyborg_server.services.session_agenda_service import SessionAgendaService
from cyborg_server.services.llm_dispatch import LLMDispatchService
from cyborg_server.services.workspace_tools import make_workspace_tools
```

This creates tight coupling: `VoiceService` knows how to construct `DispatchService`, `LLMDispatchService`, and `SessionAgendaService`. Any constructor change to any of those ripples through callers.

**Impact:** Hard to test services in isolation. Hard to swap implementations. Circular dependency risk grows with each inline import.

**Recommendation:** Use dependency injection via the `AppContext` object (which already exists but is underutilized for this purpose). Register service factories there and resolve dependencies through it.

---

## 7. Tool Definitions Scattered Across Files

LLM tools (function-calling schemas) are defined in multiple places:

| File | Tools |
|------|-------|
| `services/tools.py` | Core tools (workspace, search, etc.) |
| `services/contact_tools.py` | Contact management |
| `services/email_tools.py` | Email send/reply/skip |
| `services/phone_tools.py` | Phone call tools |
| `services/delegation_tools.py` | Claude Code delegation |
| `services/whatsapp_outreach_tools.py` | WhatsApp outreach |
| `services/project_tools.py` | Project management |
| `services/workspace_tools.py` | File/workspace access |

There's no registry or manifest. Each service's tool module is imported ad-hoc by the services that need them. Some tools (like `send_whatsapp_message`) are defined as closures inside `WhatsAppBridgeService`, not as registered tools at all.

**Impact:** No way to see all available tools in one place. Tools defined as closures can't be reused or tested independently.

**Recommendation:** Create a `ToolRegistry` that all tool modules register with. Each tool module exports a `register(registry)` function. The LLM dispatch layer queries the registry instead of importing tool modules directly.

---

## 8. Inconsistent Naming Conventions

| Inconsistency | Examples |
|---------------|----------|
| **Service vs. Service** | `harness_service.py` (the LLM orchestrator), `voice_service.py` (the voice pipeline), `session_service.py` (message storage) — all "services" with vastly different scopes |
| **Router naming** | `dashboard_api.py` and `dashboard_ws.py` split one concern; `openclaw.py` and `context.py` overlap |
| **Tool file naming** | Some use `_tools.py` suffix, others don't (`tools.py`, `workspace_tools.py`, `phone_tools.py` vs `skill_loader.py`, `skill_env.py`) |
| **Session key components** | `agent:main:whatsapp:dm:+1234567890` uses `+` prefix for phone, but contacts table stores bare numbers — normalization is ad-hoc |
| **Model vs. Table** | `models.py` is 1200+ lines mixing Pydantic models, SQLAlchemy hints, enum-like Literals, and schema constants — no clear separation |

**Recommendation:** Adopt consistent naming:
- Service files: `{domain}_service.py` (most already follow this)
- Tool files: `{domain}_tools.py` (already consistent)
- Router files: one router per domain prefix (split dashboard into `dashboard/` package if needed)
- Phone normalization: centralize in one function used by all services

---

## 9. Overly Large Files

| File | Approximate Lines | Concern |
|------|-------------------|---------|
| `models.py` | 1200+ | Pydantic models, Literals, enums, schema constants all mixed |
| `cli.py` | 3300+ | Every CLI command in one file |
| `phone.py` | 900+ | Router + business logic + Twilio integration + recording proxy |
| `whatsapp_bridge_service.py` | 700+ | Bridge connection + message handling + tool definitions + media processing |

**Impact:** Large files resist change. `cli.py` at 3300 lines means any CLI change has a high cognitive load. `models.py` at 1200 lines means schema changes risk breaking unrelated models.

**Recommendation:** Split by domain:
- `models/` package: `session.py`, `contact.py`, `task.py`, `notification.py`, etc.
- `cli/` package: `cli_tasks.py`, `cli_phone.py`, `cli_projects.py`, etc.
- `phone.py`: extract `_CallRecorderProxy` and inbound setup into `services/phone_call_service.py`

---

## 10. Missing: Comprehensive Error Handling Strategy

Error handling is inconsistent:
- Some services catch all exceptions and log (`voice_service.py`)
- Some let exceptions propagate to the router layer
- WebSocket handlers have no standardized error protocol
- Background tasks can fail silently if the event bus isn't listening

**Recommendation:** Define an application-wide error hierarchy. Services should raise domain-specific exceptions. Routers and WebSocket handlers should catch and translate to appropriate HTTP/WS responses.

---

## Priority Matrix

| Issue | Effort | Impact | Priority |
|-------|--------|--------|----------|
| ~~session_routes CHECK constraint~~ | ~~Small~~ | ~~High~~ | **Done** |
| ~~Dual message storage~~ | ~~Medium~~ | ~~High~~ | **Done** |
| ~~Session key format alignment~~ | ~~Medium~~ | ~~High~~ | **Done** |
| OpenClaw → generic renaming | Medium | Medium | Ongoing refactor |
| Repository layer introduction | Large | High | Quarterly initiative |
| Service coupling / DI | Large | Medium | Quarterly initiative |
| Tool registry | Medium | Medium | When tools change next |
| File splitting (cli, models) | Medium | Low | Gradual |
| Naming consistency | Small | Low | As touched |
| Error handling strategy | Large | Medium | When reliability becomes a focus |
