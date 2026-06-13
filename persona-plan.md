# Embed Core Persona in Codebase

## Context

Currently Bob's persona is loaded from workspace files (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`, `USER.md`) which live in `~/.config/cyborg/harness/`. These are user-editable, which means:
- The agent itself could modify its own persona (security risk)
- Other users can't get Bob's personality without manually creating these files

Goal: embed the core persona in the codebase as a template, with deployment-specific values drawn from a database table. USER.md stays on disk.

## Approach

### 1. New migration: `schemas/340_persona_config.sql`

Create a `persona_config` table for template variables:

```sql
CREATE TABLE IF NOT EXISTS persona_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Seed defaults
INSERT OR IGNORE INTO persona_config (key, value) VALUES
    ('owner_name', 'Mike'),
    ('model', 'OpenAI 5.4 mini'),
    ('channel', 'WhatsApp'),
    ('host', 'mike-workstation');
```

### 2. Create `packages/cyborg-server/cyborg_server/services/persona.py`

New module with string constants for SOUL, IDENTITY, and AGENTS content. The IDENTITY string uses `{key}` placeholders for DB-configured values:

- `{owner_name}` — replaces "Mike" in identity text
- `{model}` — replaces "OpenAI 5.4 mini"
- `{channel}` — replaces "WhatsApp (primary)"
- `{host}` — replaces "mike-workstation"

The module exports:
- `PERSONA_TEMPLATE` — the full persona string with `{key}` placeholders
- `async get_persona(db) -> str` — queries `persona_config` table and returns the rendered string

### 3. Modify `packages/cyborg-server/cyborg_server/services/prompt_assembler.py`

- Change `_WORKSPACE_FILES` from `("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")` to `("USER.md",)`
- Import `get_persona` from `persona.py`
- Call `get_persona(db)` to get the rendered persona (with DB values filled in)
- Initialize `parts` with `[rendered_persona]`
- The workspace file loop then only loads `USER.md` from disk
- Cache the rendered persona alongside the mtime cache (re-render only when USER.md changes — persona_config values change rarely and a service restart picks them up)
- Log warning if deprecated `SOUL.md` / `IDENTITY.md` / `AGENTS.md` exist in workspace

### 4. Update references in `skill_developer_service.py`

Two lines (106, 161) reference reading SOUL.md/IDENTITY.md — update to note persona is embedded in codebase.

### No changes needed to:
- **14+ callers** of `load_workspace_prompt()` — same function signature
- **subagent_service.py** — inherits change automatically via `load_workspace_prompt()`
- **build_chat_messages()**, skills, memory tools — untouched

## Files

| File | Action |
|------|--------|
| `schemas/340_persona_config.sql` | **NEW** — migration for `persona_config` table |
| `services/persona.py` | **NEW** — embedded persona template + `get_persona()` |
| `services/prompt_assembler.py` | **MODIFY** — use embedded persona, only load USER.md |
| `services/skill_developer_service.py` | **MODIFY** — update 2 text references |

## Verification

1. Restart: `systemctl --user restart cyborg.service`
2. Confirm migration applied — check `persona_config` table exists with seeded values
3. Send a test WhatsApp message — verify Bob's personality is intact
4. Check system prompt in logs includes the rendered persona
5. Update a value in `persona_config` (e.g. change model), restart, verify it appears in prompt
6. Test with no workspace files — Bob should still have full persona (minus USER.md content)
