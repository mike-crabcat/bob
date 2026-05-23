# Memory Wiki System

The memory wiki gives Cyborg a persistent, structured knowledge base that survives across conversations. Without memory, every session starts from scratch -- the LLM has no recollection of facts, people, or preferences discussed in prior exchanges. The memory system closes this gap by letting the assistant record useful information during and after conversations, then automatically surfacing it in future prompts.

Memory is organized as a file-based wiki stored under the workspace directory. A lightweight index is always injected into the system prompt, so the assistant knows what it knows without extra tool calls. When it needs detail, it uses on-demand tools to read or search entries. When it learns something worth remembering, it writes an entry. A background reflection process also reviews completed conversations and extracts facts automatically.

## Architecture Overview

```
                              Prompt Assembly
                              ===============
                              +-----------+
                              | SOUL.md   |
                              | IDENTITY  |
                              | AGENTS.md |
                              | USER.md   |
                              | Skills    |
                              |  Index    |
                              +-----+-----+
                                    |
                     +--------------+--------------+
                     |                             |
              +------+------+              +-------+-------+
              | Memory Index |              | Grounding     |
              | (_index.md)  |              | Rules         |
              +------+------+              +---------------+
                     |                              |
                     | always-accessible wikis       |
                     | loaded into every prompt      |
                     |                              |
              +------+------+
              |  System     |
              |  Prompt     |
              +------+------+
                     |
         +-----------+-----------+
         |   Session History     |
         +-----------+-----------+
                     |
         +-----------+-----------+
         |   User Message        |
         +-----------+-----------+
                     |
              +------+------+
              |  LLM Dispatch|
              |  + Tools     |
              +------+------+
                     |
         +-----------+-----------+-----------+
         |           |           |           |
    memory_write  memory_read  memory_search memory_browse
         |           |           |             |
         v           v           v             v
   +-----------------------------------------------+
   |           memory/ (filesystem)                |
   |  access.yml                                   |
   |  core/                                         |
   |    _index.md                                   |
   |    people/   facts/   events/   locations/     |
   |      alice.md  ...      ...        ...         |
   +-----------------------------------------------+
```

```
  Post-Session Reflection Flow
  ============================

  Session ends (idle timeout)
        |
        v
  SessionIdleSummaryTask (heartbeat.py)
        |
        v
  SessionSummaryService.generate_summary()
        |
        +--> summary_text
        +--> memory_prompts[]
        |
        v
  store_summary() to DB
        |
        v
  MemoryService.reflect_and_update()
        |
        v
  LLM call: review summary + prompts
        |
        v
  JSON operations: [{action:"write", wiki, category, slug, title, content}]
        |
        v
  write_entry() --> memory/<wiki>/<category>/<slug>.md
        |
        v
  rebuild_wiki_index() --> memory/<wiki>/_index.md
```

## File-Based Storage

All memory data lives under `<workspace_dir>/memory/`. There is no database table for entries -- everything is a markdown file on disk, versioned alongside the workspace.

### Directory Structure

```
memory/
  access.yml               # Wiki configuration and access control
  core/                    # Default wiki (created automatically)
    _index.md              # Auto-generated compact index
    people/                # Category directories
      alice-johnson.md
      bob-smith.md
    facts/
      coffee-preference.md
      timezone.md
    events/
      trip-to-perth.md
    locations/
      office-address.md
    research/
      project-alpha.md
```

The `memory/` directory and default `core` wiki structure are created automatically by `MemoryService.ensure_memory_structure()` the first time the system starts or when the prompt assembler runs.

### access.yml

The `access.yml` file defines wikis, their categories, and access policies. If it does not exist when the system starts, a default is created automatically from the `_DEFAULT_ACCESS_YML` constant in `memory_service.py`.

```yaml
wikis:
  core:
    description: "General knowledge"
    categories: [people, facts, events, locations, research]
    access: always
    write: always
```

Each wiki has these fields:

- `description` -- human-readable label
- `categories` -- list of category names; each becomes a subdirectory under the wiki directory
- `access` -- read access level: `always`, `trusted`, or `never`
- `write` -- write access level: `always`, `trusted`, or `never`

The config is parsed with `yaml.safe_load()` and cached in a module-level variable (`_config_cache`) with mtime-based invalidation, so edits to `access.yml` take effect on the next prompt assembly cycle without a restart.

### Per-Wiki Indexes (_index.md)

Each wiki has an auto-generated `_index.md` file that summarizes its contents in a compact format. The index is rebuilt every time an entry is written or updated via `rebuild_wiki_index()`.

The rebuild process:
1. Reads the wiki config to get the ordered list of categories.
2. For each category, scans its directory for `.md` files (skipping any starting with `_`).
3. Parses each entry to extract the title (from the `# ` heading) and a one-line summary (first non-heading paragraph).
4. Formats each entry as `slug (title, summary...)`.
5. Writes the combined output to `memory/<wiki>/_index.md`.

Example `_index.md`:

```markdown
### core
**people**: alice-johnson (Alice Johnson, Software engineer at TechCorp), bob-smith (Bob Smith, Prefers dark roast coffee)
**facts**: coffee-preference (Coffee preference, Only drinks pour-over), timezone (Timezone, UTC+8 Australia/Perth)
**events**: trip-to-perth (Trip to Perth, Planned for March 2025)
```

The index for `always`-access wikis is loaded into every system prompt, so the assistant always knows what memory entries exist. This is intentionally lightweight -- just slugs, titles, and truncated summaries (max 80 chars). Full content is retrieved on demand via `memory_read`.

## Authoring Memory Entries

### Real-Time: memory_write Tool

During an active conversation, the LLM can use the `memory_write` tool to create or update entries immediately. This is the fastest path -- the assistant decides a fact is worth recording and writes it in the same turn.

Parameters:

| Parameter  | Description                                          |
|------------|------------------------------------------------------|
| `wiki`     | Wiki name (must exist in `access.yml`)               |
| `category` | Category within the wiki                             |
| `slug`     | URL-safe identifier (lowercase, hyphens, no spaces)  |
| `title`    | Human-readable title                                 |
| `content`  | Markdown body                                        |

The tool validates that the wiki and category are defined in `access.yml`, checks write access via `resolve_writable_wikis()`, writes the file using `write_entry()`, and rebuilds the wiki index. The resulting file is formatted as:

```markdown
# <title>

<content>
```

The workspace's `write_file` tool is guarded to reject writes into `memory/` -- all modifications must go through `memory_write` to keep indexes consistent. The guard in `workspace_tools.py` checks if the resolved path starts with `workspace/memory` and returns an error message directing the caller to use `memory_write` instead.

### Post-Session: Reflection via Heartbeat

After a conversation goes idle, the heartbeat system generates a summary and extracts `memory_prompts` -- a list of facts worth remembering. The flow is:

1. `SessionIdleSummaryTask` (registered in `heartbeat.py`) runs on each heartbeat cycle.
2. It calls `SessionSummaryService.find_idle_sessions()` to detect sessions with no recent activity beyond the configured idle threshold.
3. For each idle session, it fetches messages and participants, then calls `generate_summary()`.
4. The summary LLM call produces `summary_text`, `topics`, and `memory_prompts` (a list of specific facts or action items worth remembering).
5. The summary is stored in the `session_summaries` database table.
6. If `memory_prompts` is non-empty, `MemoryService.reflect_and_update()` is called.

The reflection process in `reflect_and_update()`:
1. Resolves which wikis are writable for the session.
2. Builds the current memory index so the LLM can see what already exists.
3. Sends the summary and memory prompts to an LLM with instructions to produce a JSON array of write operations: `[{"action": "write", "wiki": "...", "category": "...", "slug": "...", "title": "...", "content": "..."}]`.
4. Validates each operation (wiki exists in writable set, category is valid per config, all fields are non-empty).
5. Calls `write_entry()` for each valid operation, which writes the file and rebuilds the wiki index.

This means memory grows organically from conversation content without requiring explicit user action. The LLM decides what is genuinely useful and avoids duplicating existing entries by showing it the current index.

## Retrieval

### Lightweight Index (Always in Prompt)

The `prompt_assembler` module calls `MemoryService._build_memory_index_static()` during prompt construction in `load_workspace_prompt()`. The process:

1. `ensure_memory_structure()` is called to guarantee the directory exists.
2. `load_access_config()` reads `access.yml`.
3. Wiki names with `access: always` are collected.
4. For each always-accessible wiki, its `_index.md` is read.
5. All index contents are joined with a header instructing the assistant to use `memory_read` for full details and `memory_search` to find entries.
6. The result is appended under a `## Memory` heading in the system prompt.

This is a zero-overhead path -- no tool call, no extra latency. The assistant starts every turn knowing what it knows.

### memory_read Tool

Reads a single entry by wiki, category, and slug. Returns the full markdown content (title heading + body). Checks that the session has read access to the requested wiki via `resolve_accessible_wikis()`. Returns an error JSON if access is denied or the entry is not found.

### memory_search Tool

Semantic search across one or all accessible wikis. The search is LLM-powered:

1. Collects all entries (excluding files starting with `_`) from the target wikis by walking the directory tree with `rglob("*.md")`.
2. Builds a catalog with full text (truncated to 500 chars per entry), titles, summaries, and workspace-relative paths.
3. Sends the catalog and query to an LLM with a strict system prompt requesting a JSON response with `abstract` (1-2 sentence summary) and `results` (array of matched entries with index numbers and relevance explanations).
4. Falls back to keyword matching across title, summary, and full text if the LLM response is not valid JSON.
5. Maps index numbers back to entry paths and titles.

Returns `{abstract, results}` where each result has `path` (workspace-relative, e.g. `memory/core/people/alice-johnson.md`), `title`, and `relevance` (a sentence explaining why it matched). The assistant can use `read_file` with the path to get the full document.

Every search is logged to the `memory_search_log` database table (see Search Logging below).

### memory_browse Tool

Lists all entries in a wiki category. Returns a JSON array of `{slug, title, modified}` sorted alphabetically by filename. Useful for exploring what exists in a category before searching.

## Access Control

Each wiki has independent read (`access`) and write (`write`) policies:

| Level    | Behavior                                                              |
|----------|-----------------------------------------------------------------------|
| `always` | Accessible to all sessions, including unauthenticated ones            |
| `trusted`| Only accessible when `session_participants.is_trusted = 1` for the session |
| `never`  | Never accessible (reserved for future use)                            |

The trust check queries the `session_participants` table:

```sql
SELECT 1 AS ok FROM session_participants
WHERE session_key = ? AND is_trusted = 1 LIMIT 1
```

Access is resolved per-request in two methods:
- `resolve_accessible_wikis()` -- determines which wikis the session can read.
- `resolve_writable_wikis()` -- determines which wikis the session can write to.

Both methods iterate the `access.yml` config and check the appropriate field (`access` or `write`) against the trust level. The `memory_write`, `memory_read`, `memory_search`, and `memory_browse` tools all check access before performing any operation.

## CLI: cyborg memory seed

The `cyborg memory seed` command bulk-processes historical session summaries to populate the memory wiki retroactively. This is useful when the memory system is first enabled on an existing installation with accumulated session data.

```bash
# Dry run -- see what would be processed without calling the LLM
cyborg memory seed --dry-run

# Process in batches of 10 summaries per LLM call
cyborg memory seed --batch-size 10
```

The command:

1. Loads settings and connects to the database.
2. Calls `MemoryService.ensure_memory_structure()` to create the directory if needed.
3. Queries `session_summaries` for rows with non-empty `memory_prompts`.
4. Groups summaries into batches (by insertion order, configurable size via `--batch-size`).
5. For each batch, combines summaries (up to 5 summary texts) and collects all memory prompts.
6. Calls `reflect_and_update()` with a synthetic `bulk_seed` session key (treated as a trusted session).
7. The reflection LLM decides which facts to write, just like the real-time post-session path.
8. Prints a summary and the current memory index.

In dry-run mode, the LLM is not called -- the command just lists the prompts that would be processed.

## Search Logging

Every `memory_search` call (from tool or dashboard) is logged to the `memory_search_log` table. The schema is defined in `schemas/300_memory_search_log.sql`:

```sql
CREATE TABLE IF NOT EXISTS memory_search_log (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    results_json TEXT NOT NULL DEFAULT '[]',
    session_key TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    latency_seconds REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_search_log_created
    ON memory_search_log(created_at DESC);
```

Fields:

| Column            | Description                                           |
|-------------------|-------------------------------------------------------|
| `id`              | UUID primary key                                      |
| `query`           | The search query string                               |
| `results_json`    | Full JSON response (abstract + results array)         |
| `session_key`     | Session that initiated the search (null for dashboard)|
| `result_count`    | Number of results returned                            |
| `latency_seconds` | Wall-clock time for the search operation              |
| `created_at`      | Timestamp                                             |

Logging is performed in the `memory_search` tool after the search completes. Failures to log are caught and logged at debug level to avoid disrupting the search response. The same logging happens in the dashboard search endpoint, with `session_key` set to `null`.

This table powers the dashboard memory page's history view and is useful for understanding what the assistant searches for and how effective retrieval is.

## Dashboard Memory Page

The dashboard includes a memory page at `/memory` with two features:

1. **Live search** -- An input field that calls `GET /api/memory/search?q=...` and displays results inline with abstract, relevance explanations, and latency.
2. **Search history** -- Fetches the last 100 entries from `GET /api/memory/searches` and displays them as an expandable list showing query, result count, latency, and relative time.

Dashboard API endpoints (defined in `routers/dashboard_api.py`):

- `GET /api/memory/searches` -- Returns the last 100 rows from `memory_search_log` with parsed results (abstract and results array extracted from `results_json`).
- `GET /api/memory/search?q=...` -- Runs a memory search against the `core` wiki (dashboard always searches `core`), logs the result to `memory_search_log` with `session_key = null`, and returns the result with `latency_seconds` appended.

Both endpoints are protected by the dashboard secret (Bearer token or `?secret=` query parameter) if one is configured.

## Key Source Files

| File | Purpose |
|------|---------|
| `services/memory_service.py` | Core service: CRUD, index building, search, reflection, config loading |
| `services/memory_tools.py` | LLM function-call tools (memory_write, memory_read, memory_search, memory_browse) |
| `services/prompt_assembler.py` | Injects memory index into system prompt for always-accessible wikis |
| `services/workspace_tools.py` | Guards `memory/` directory from direct write_file access |
| `services/session_summary_service.py` | Generates summaries with memory_prompts from session history |
| `heartbeat.py` | SessionIdleSummaryTask triggers reflection after summaries are stored |
| `cli.py` | `cyborg memory seed` command for bulk processing historical summaries |
| `schemas/300_memory_search_log.sql` | Database schema for search logging |
| `routers/dashboard_api.py` | Dashboard API endpoints for memory search and search history |
| `ui_app/src/routes/memory/index.tsx` | Dashboard memory page UI component |
