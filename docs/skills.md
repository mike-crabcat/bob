# Skills System

## Purpose

Skills are Bob's plugin mechanism for teaching the LLM agent how to perform structured, multi-step actions on the user's behalf — fetching weather data, searching for nearby places, generating images, converting documents, drafting and sending email, and so on. Each skill is a small directory of files dropped into the workspace. The LLM discovers skills through a lightweight index injected into every system prompt and loads the full instructions on-demand only when a user request matches a skill's trigger.

The design goals are:

- **Zero-code registration**: drop a directory with a `skill.md` into `skills/` and it is automatically discovered on the next turn.
- **Lazy loading**: only the index (name, description, trigger) is loaded into the system prompt; full instructions are loaded when the LLM calls `use_skill`.
- **Hot reload**: caches are keyed on file modification times, so skills can be added, edited, or removed without restarting the server.
- **Sandboxed execution**: helper Python scripts run through the `bash` tool in a subprocess whose environment maps `BOB_`-prefixed secrets to the standard variable names that third-party SDKs expect, with regex guardrails keeping commands inside the workspace.

---

## File Structure

Each skill is a directory under `<workspace>/skills/` (workspace default: `~/workspace`, override with `BOB_HARNESS_WORKSPACE_DIR`). The directory name is the skill identifier used everywhere — the index, `use_skill`, and the dashboard all key off it. A skill directory must contain a `skill.md` (or `SKILL.md`) file; everything else is optional.

```
skills/
  bom-weather/                  # Skill directory (name = "bom-weather")
    skill.md                    # Required: frontmatter + instructions
    bom_fetcher.py              # Optional: helper script
    weather_formatter.py        # Optional: helper script
    README.md                   # Optional: human-readable docs

  google-places/                # Skill with helper scripts
    skill.md
    geocoder.py
    places_search.py
    places_formatter.py

  changelog-impact/             # Instruction-only skill (no scripts)
    skill.md
```

### Required files

| File | Purpose |
|------|---------|
| `skill.md` or `SKILL.md` | Frontmatter metadata + markdown instructions for the LLM. Either filename is accepted; `skill.md` is checked first. |

### Optional files

| File | Purpose |
|------|---------|
| `*.py` | Helper Python scripts invoked by the LLM through the `bash` tool. The authoring convention (see `skill_developer_service.py`) names the main script `helper.py`, and skill instructions typically read `bash("python skills/<name>/helper.py …")`. Any script filename works as long as `skill.md` tells the LLM how to run it. |
| `pyproject.toml` | Recognised by the dashboard (`has_pyproject` flag) as declaring dependencies. Note: the current runtime uses a single shared Python venv (`~/bobenv`) for all skills — see "Script execution environment" below — so per-skill dependency files are a convention the UI flags rather than something the loader enforces. |
| `README.md` | Human-readable documentation (not consumed by the system). |

> Note: the dashboard's `/api/skills/installed` endpoint checks for a file literally named `helper.py` when reporting `has_helper` — that field is a convention check, not a hard requirement.

---

## Skill File Format (`skill.md`)

A skill file has two parts: a YAML-like frontmatter block and a markdown body.

### Frontmatter

Delimited by `---` on their own lines at the top of the file. Contains key-value pairs in `key: value` format. This is not full YAML — the parser (`skill_loader._parse_frontmatter`) matches the block with a regex and splits each line on the first colon, stripping whitespace. No nested structures, lists, or quoting are supported.

```markdown
---
name: google-places
description: Search for nearby places using Google Places API for holiday planning and travel research
trigger: when Bob asks to find, search for, or discover places, attractions, restaurants, cafes, parks, or activities near a location
---
```

Supported keys:

| Key | Purpose |
|-----|---------|
| `name` | Skill identifier. Parsed but effectively informational — the system keys skills by directory name everywhere (index entries, `use_skill`, dashboard). |
| `description` | One-line summary shown in the system prompt index. |
| `trigger` | Natural language description of when the LLM should activate this skill. The LLM reads this from the index to decide whether to call `use_skill`. |

### Body

The markdown body after the frontmatter is the full instruction set loaded on-demand when the LLM calls the `use_skill` tool. It tells the LLM exactly how to execute the skill: what scripts to run, what arguments to pass, how to interpret results, and how to respond to the user.

Example body structure:

```markdown
## Instructions

When this skill activates:

1. **Fetch Data**: Call `bash("python skills/bom-weather/bom_fetcher.py --location 'Perth Metro'")`
2. **Format Output**: Call `bash("python skills/bom-weather/weather_formatter.py --json '<fetcher output>'")`
3. **Send to User**: Use `send_whatsapp_to_contact` with the formatted result.

## Error Handling
- If data unavailable: inform user and suggest retry
```

> Note on `run_script` references: some older skills and documentation reference a `run_script` tool that executed helpers via `uv run`. That tool no longer exists in the runtime. The actual, registered tool for running skill scripts is `bash`, and dependencies resolve through the shared `~/bobenv` venv rather than per-skill `uv` environments. When the LLM encounters a stale `run_script(...)` instruction it generally translates it into the equivalent `bash` invocation, but authoring skills with `bash` directly is the correct, current convention.

---

## Architecture

The system uses a three-stage pipeline: discover → index → activate.

### Stage 1 — Discovery and Index Injection

```
                     SKILL DISCOVERY AND INDEX INJECTION

  +--------------------------------+
  |  <workspace>/skills/           |
  |    +- bom-weather/             |
  |    |    skill.md               |
  |    +- google-places/           |
  |    |    skill.md               |
  |    +- changelog-impact/        |
  |         skill.md               |
  +---------------+----------------+
                  |
                  | skill_loader._scan_skills()
                  |   iterate skills/* dirs, check skill.md
                  |   then SKILL.md, record each mtime
                  v
  +--------------------------------+
  | {skill_name: mtime} map        |
  | mtime_hash = tuple(sorted(     |
  |   mtimes.items()))             |
  +---------------+----------------+
                  |
                  | hash matches _index_cache?
          +-------+--------+
          | yes           | no
          v               v
  +--------------+  +--------------------------------+
  | return cached|  | rebuild: read each skill.md,  |
  | index string |  | _parse_frontmatter(), emit    |
  +--------------+  | "- **name** (skills/name/):   |
          ^         |    <description>"             |
          |         | "  Trigger: <trigger>"        |
          |         | store in _index_cache         |
          |         +----------------+---------------+
          |                          |
          +------------+-------------+
                       |
                       | prompt_assembler.load_workspace_prompt()
                       |   appends under "## Available Skills"
                       v
  +--------------------------------+
  |  System prompt (every turn)    |
  |  ...                           |
  |  ## Available Skills           |
  |  When a skill trigger matches  |
  |  the user's request, call the  |
  |  `use_skill` tool ...          |
  |  - **bom-weather** (skills/    |
  |    bom-weather/): ...          |
  |    Trigger: ...                |
  +--------------------------------+
```

The flow works as follows:

1. **Scan**: `skill_loader._scan_skills()` iterates over `skills/`, looking for directories that contain `skill.md` or `SKILL.md` (`skill.md` checked first). It returns a `{skill_name: mtime}` mapping. Directories without either file are ignored.

2. **Cache check**: `load_skills_index()` hashes the mtimes into a tuple of sorted `(name, mtime)` pairs. If the hash matches the cached index, the cached string is returned immediately. Otherwise the index is rebuilt.

3. **Build index**: For each skill, the frontmatter is parsed to extract `description` and `trigger`. A compact text index is assembled with one entry per skill, prefixed with a preamble telling the LLM to call `use_skill` when a trigger matches.

4. **Inject into prompt**: `prompt_assembler.load_workspace_prompt()` calls `load_skills_index()` and appends the result under a `## Available Skills` heading. This section is included in every system prompt sent to the LLM, so the agent sees the catalogue of available skills on every turn. The workspace prompt itself is also cached by an mtime hash that includes the skill files, so newly added skills invalidate that cache automatically.

### Stage 2 — On-Demand Skill Activation

```
                     ON-DEMAND SKILL ACTIVATION

  User message:
   "What's the weather in Perth?"

  +--------------------------------+
  |  LLM reads index in system     |
  |  prompt; bom-weather trigger   |
  |  matches the request           |
  +---------------+----------------+
                  |
                  | use_skill("bom-weather")
                  v
  +--------------------------------+
  | workspace_tools.use_skill()    |
  |   -> skill_loader.load_skill() |
  |   reads full skill.md content, |
  |   returns it prefixed with     |
  |   "Skill: <name>" and          |
  |   "Path: <resolved dir>/"      |
  +---------------+----------------+
                  |
                  | LLM reads full instructions
                  v
  +--------------------------------+
  | bash("python skills/           |
  |   bom-weather/bom_fetcher.py   |
  |   --location 'Perth Metro'")   |
  +---------------+----------------+
                  |
                  | _check_command_safety() regex guards
                  v
  +--------------------------------+
  | bash tool: bash -c <command>   |
  |   cwd  = workspace root        |
  |   env  = skill_env.            |
  |          build_skill_env(      |
  |            workspace_dir=...,  |
  |            venv_dir=~/bobenv)  |
  |   timeout = 900s               |
  |   output > 30000 chars         |
  |     truncated                  |
  +---------------+----------------+
                  |
                  | stdout returned as tool result
                  v
  +--------------------------------+
  |  LLM formats the response and  |
  |  delivers it via a messaging   |
  |  tool (send_whatsapp_message,  |
  |  email_reply, ...)             |
  +--------------------------------+
```

The activation path:

1. **Trigger match**: The LLM reads the skills index in its system prompt. When a user message matches a trigger description, the LLM calls the `use_skill` tool with the skill name.

2. **Load instructions**: `use_skill` (defined in `workspace_tools.py`) calls `skill_loader.load_skill()`, which reads the full `skill.md` content and returns it prefixed with the skill name and resolved directory path, so the LLM can construct correct `bash` commands. An unknown name returns `Error: skill '<name>' not found`.

3. **Execute scripts**: The LLM follows the instructions, calling `bash` for any helper Python scripts (conventionally `bash("python skills/<name>/helper.py …")`). Details of the execution environment are in the next section.

4. **Respond**: The LLM uses the script output to compose a response and deliver it via the appropriate messaging tool (e.g., `send_whatsapp_message`, `send_whatsapp_to_contact`, or `email_reply`).

### Script execution environment

Every `bash` call from `workspace_tools.py` runs `bash -c <command>` with:

- **cwd**: the workspace root, so relative paths like `skills/<name>/helper.py` land correctly.
- **env**: `skill_env.build_skill_env()`, which:
  - maps configured `BOB_`-prefixed secrets to the standard variable names third-party SDKs expect — `BOB_OPENAI_API_KEY` → `OPENAI_API_KEY`, `BOB_OPENAI_BASE_URL` → `OPENAI_BASE_URL`, `BOB_AGENTMAIL_API_KEY` → `AGENTMAIL_API_KEY`, `BOB_GOOGLE_PLACES_API_KEY` → `GOOGLE_PLACES_API_KEY`, `BOB_GIPHY_API_KEY` → `GIPHY_API_KEY` (only when the `BOB_` var is set and non-empty);
  - injects `BOB_WORKSPACE_DIR` with the absolute workspace path;
  - activates Bob's shared Python venv (`harness.venv_dir`, default `~/bobenv`, created at startup by `config._ensure_venv()` if missing) the way sourcing `activate` would: sets `VIRTUAL_ENV`, prepends the venv's `bin` to `PATH`, and clears `PYTHONHOME`. If the venv binary is missing, `PATH` is left untouched so commands still run.

  Skill scripts can therefore read `os.environ.get("OPENAI_API_KEY")` etc. without knowing about the `BOB_` prefix. Because all skills share one venv, `pip install <pkg>` in a skill lands in `~/bobenv` for every skill — skills do not get their own per-skill venvs. Skill instructions that need a third-party package tell the LLM to install it once (`pip install requests`) or, preferably, stick to the standard library.
- **sandbox guardrails**: `_check_command_safety()` screens every command with layered regex checks — direct database clients (`sqlite3`, `psql`, …), references to the bob DB file or `BOB_DB_PATH`, privilege escalation (`sudo`/`su`/…), the configured DB/data/config directories, sensitive system paths (`/etc`, `~/.ssh`, `.env`, …), and `..` path traversal. Blocked commands return a `BLOCKED: …` error instead of executing. The system prompt carries matching language.
- **limits**: 900-second timeout; output above 30,000 characters is truncated with a pointer to `head`/`tail`/`sed -n`/`grep` for paging. Non-zero exit codes return `Error (exit code N):` followed by stderr (or stdout).

---

## Component Reference

### `skill_loader.py`

Path: `packages/bob-server/bob_server/services/skill_loader.py`

Functions:

- **`_parse_frontmatter(text)`** — Extracts YAML-like `key: value` pairs from the `---`-delimited block at the top of the text. Splits each line on the first colon. Used by the loader and by the dashboard endpoint.
- **`_scan_skills(workspace_dir)`** — Returns `{skill_name: mtime}` for every directory under `skills/` that contains a `skill.md` or `SKILL.md` (checking `skill.md` first).
- **`load_skills_index(workspace_dir)`** — Returns a compact index string for the system prompt. Cached at module level by mtime hash. Prepended with a preamble instructing the LLM to call `use_skill` when a trigger matches. Returns `""` when no skills exist.
- **`load_skill(workspace_dir, skill_name)`** — Returns the full content of a single skill's `skill.md`, prefixed with `Skill: <name>` and `Path: <resolved dir>/`. Used by the `use_skill` tool.
- **`load_skills_prompt(workspace_dir)`** — Returns the concatenated content of all skills (used for diagnostics/testing).

Module-level caches:

- `_index_cache: tuple[Any, str] | None` — keyed by mtime hash; holds the compact index.
- `_skills_cache: tuple[Any, str] | None` — keyed by mtime hash; holds the full concatenated content.

### `workspace_tools.py`

Path: `packages/bob-server/bob_server/services/workspace_tools.py`

`make_workspace_tools(ctx, *, session_key=None)` returns the tool set bound to the application context. It is registered into every session's tool surface by `tool_registry.build_common_tools()`. The skill-related tools are:

- **`use_skill(skill_name)`** — Loads full instructions for a named skill via `skill_loader.load_skill()`. Returns the skill markdown content prefixed with the skill name and resolved directory path, so the LLM can follow the skill's steps and construct correct `bash` commands.
- **`bash(command)`** — Runs a bash command via `bash -c` with `cwd` set to the workspace root, the sandboxed skill environment from `build_skill_env()` (secret mapping + shared venv activation), regex safety screening, a 900s timeout, and 30,000-char output truncation. This is the tool skill scripts run through.
- **`read_image(path)`** — Loads an image from the workspace for vision (not skill-specific, but available alongside the above).

There is no `run_script` tool. The runtime tool surface for executing skill scripts is `bash` only.

### `skill_env.py`

Path: `packages/bob-server/bob_server/services/skill_env.py`

`build_skill_env(base_env=None, *, workspace_dir=None, venv_dir=None)` builds the environment dict for skill subprocesses. It starts from the parent process environment (or `base_env` if provided), then:

| Injection | Behaviour |
|---|---|
| `BOB_OPENAI_API_KEY` → `OPENAI_API_KEY` | only when the BOB var is set and non-empty |
| `BOB_OPENAI_BASE_URL` → `OPENAI_BASE_URL` | ditto |
| `BOB_AGENTMAIL_API_KEY` → `AGENTMAIL_API_KEY` | ditto |
| `BOB_GOOGLE_PLACES_API_KEY` → `GOOGLE_PLACES_API_KEY` | ditto |
| `BOB_GIPHY_API_KEY` → `GIPHY_API_KEY` | ditto |
| `BOB_WORKSPACE_DIR` | absolute workspace path, when `workspace_dir` is passed |
| venv activation | when `<venv_dir>/bin/python` exists: sets `VIRTUAL_ENV`, prepends `venv/bin` to `PATH`, removes `PYTHONHOME`; otherwise leaves `PATH` untouched |

### `prompt_assembler.py`

Path: `packages/bob-server/bob_server/services/prompt_assembler.py`

`load_workspace_prompt(workspace_dir, db=None)` assembles the system prompt. It renders the embedded persona, then calls `load_skills_index()` and appends the result under a `## Available Skills` heading. This section is present in every system prompt, giving the LLM awareness of all available skills on every turn. The workspace prompt is cached at module level by an mtime hash that includes both the workspace persona files and every skill's `skill.md`, so adding or editing a skill invalidates the cache automatically.

### `dashboard_api/skills.py`

Path: `packages/bob-server/bob_server/routers/dashboard_api/skills.py` (mounted via the `dashboard_api` package router)

The **`GET /api/skills/installed`** endpoint lists every skill directory along with parsed frontmatter and two boolean convention flags:

```json
{
  "skills": [
    {
      "name": "bom-weather",
      "description": "Fetches BOM weather data for Mount Lawley/Perth ...",
      "trigger": "when Bob asks for weather ...",
      "has_helper": true,
      "has_pyproject": false
    }
  ]
}
```

`has_helper` is true when the skill directory contains a file literally named `helper.py`; `has_pyproject` is true when it contains `pyproject.toml`. These are convention flags for the UI, not requirements — the loader only requires `skill.md` or `SKILL.md`.

The dashboard also exposes skill delegation endpoints (`/api/skills/delegations`, `/api/skills/delegations/{id}`, `.../implement`, `.../reject`) for a separate skill development workflow where the LLM proposes new skills and a human approves implementation via `skill_developer_service.py`. Those are distinct from the core loading mechanism documented here. The `skill_developer_service.py` developer prompt is also the source of the current authoring conventions (helper.py format, shared-venv dependency policy, available env vars) referenced above.

---

## Caching Behavior

All caches in `skill_loader.py` (and the workspace prompt cache in `prompt_assembler.py`) are module-level variables keyed by an mtime hash derived from the modification times of all `skill.md`/`SKILL.md` files. The hash is a tuple of sorted `(name, mtime)` pairs:

```python
mtime_hash = tuple(sorted(mtimes.items()))
```

On each call to `load_skills_index()` or `load_skills_prompt()`:

1. Scan all skill directories and record each `skill.md` modification time.
2. Build the mtime hash.
3. Compare against the cached hash.
4. If identical, return the cached content.
5. If different, rebuild the content and update the cache.

This means:

- Adding a new skill directory with a `skill.md` is detected immediately on the next prompt assembly.
- Editing an existing `skill.md` is detected immediately.
- Deleting a skill directory is detected immediately.
- No server restart, no manual cache invalidation, and no configuration change is needed.

The same mtime-based invalidation also applies to the assembled workspace prompt, since `prompt_assembler` folds skill mtimes into its own cache key.

---

## Dashboard Integration

The `/api/skills/installed` endpoint provides a JSON list of all installed skills for the web dashboard (consumed by `ui_app/src/routes/skills/index.tsx`). It reuses `skill_loader._parse_frontmatter` to extract `description` and `trigger`, and reports `has_helper` / `has_pyproject` convention flags so the UI can indicate which skills ship executable code.

The dashboard's skills section is purely a view over the filesystem — there is no separate registry. Adding a skill directory to the workspace is sufficient for it to appear in both the dashboard list and the LLM's system prompt index on the next turn.

---

## Creating a New Skill

To add a new skill:

1. Create a directory under `skills/` with the desired skill name (the directory name is the identifier).
2. Add a `skill.md` file with frontmatter (`description` and `trigger`) and an instruction body.
3. If the skill needs Python scripts, add them alongside `skill.md` (convention: a single `helper.py`).
4. If scripts need third-party packages, prefer the standard library; otherwise instruct `pip install <pkg>` once — it lands in the shared `~/bobenv` venv.

No registration, no server restart. The skill appears in the LLM's index on the next incoming message.

### Minimal example (`skills/my-skill/skill.md`)

```markdown
---
name: my-skill
description: Does something useful for the user
trigger: when the user asks to do the thing
---

## Instructions

When this skill activates:

1. Do step one.
2. Do step two.
3. Report the result.
```

### Skill with a helper script (`skills/my-skill/skill.md` + `helper.py`)

```markdown
---
name: my-skill
description: Calls an external API and formats results
trigger: when the user asks for API data
---

## Instructions

1. Call `bash("python skills/my-skill/helper.py --query '<user query>'")`
2. Parse the output.
3. Send a formatted response via `send_whatsapp_message`.
```

`helper.py` conventions (from `skill_developer_service.py`): accept file paths as command-line arguments, use `argparse` or `sys.argv`, print results to stdout, exit 0 on success and non-zero on failure, be self-contained (do not assume a working directory or resolve paths internally). Secrets are available as standard env vars (`OPENAI_API_KEY`, `AGENTMAIL_API_KEY`, …) and the workspace root as `BOB_WORKSPACE_DIR`.
