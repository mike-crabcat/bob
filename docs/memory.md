# Memory Wiki System

The memory wiki gives Cyborg a persistent, structured knowledge base that survives across conversations. Without memory, every session starts from scratch -- the LLM has no recollection of facts, people, or preferences discussed in prior exchanges. The memory system closes this gap by letting the assistant record useful information during and after conversations, then automatically surfacing it in future prompts.

Memory is organized as a file-based wiki stored under the workspace directory. A lightweight index is always injected into the system prompt, so the assistant knows what it knows without extra tool calls. When it needs detail, it uses on-demand tools to read or search entries. When it learns something worth remembering, it queues a bulletin. A background dream process curates bulletins into well-structured entries organized by category. A second background flow also extracts facts from completed conversations automatically.

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
         v           |           v             v
   +----------+      |    +------------+  +----------+
   | bulletin |      |    | LLM-powered|  | category |
   | queue    |      |    | semantic   |  | listing  |
   +----+-----+      |    | search     |  +----------+
        |            |    +------+-----+
        |            |           |
        |            |     search logged to
        |            |     memory_search_log
        |            |
        |            v
        |    +-----------------+
        |    | memory/         |
        |    |  (filesystem)   |
        |    +-----------------+
        |
        v
  Heartbeat (SessionIdleSummaryTask)
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
  write_bulletin() --> memory/core/bulletins/blt-xxxx.md
        |
        v
  MemoryService.run_dream()
        |
        v
  LLM call: curate bulletins into structured entries
        |
        v
  write_entry() --> memory/core/<category>/<slug>.md
  move_to_digested() --> memory/core/digested/
        |
        v
  rebuild_wiki_index() --> memory/core/_index.md
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
    bulletins/             # Incoming queue for uncurated facts
      blt-a1b2c3d4e5f6.md
    digested/              # Processed bulletins (archive)
      blt-f9e8d7c6b5a4.md
```

The `memory/` directory and default `core` wiki structure are created automatically by `MemoryService.ensure_memory_structure()` the first time the system starts or when the prompt assembler runs.

### access.yml

The `access.yml` file defines wikis, their categories, and access policies. If it does not exist when the system starts, a default is created automatically from the `_DEFAULT_ACCESS_YML` constant in `memory_service.py`.

```yaml
wikis:
  core:
    description: "General knowledge"
    categories: [people, facts, events, locations, research, bulletins, digested]
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

During an active conversation, the LLM can use the `memory_write` tool to queue a bulletin for a fact worth recording. This is the fastest path -- the assistant decides a fact is worth remembering and writes it in the same turn. The bulletin is not immediately placed in a curated category; instead, it lands in the `bulletins/` queue to be processed by the dream process.

Parameters:

| Parameter  | Description                                          |
|------------|------------------------------------------------------|
| `wiki`     | Wiki name (must exist in `access.yml`)               |
| `category` | Intended category within the wiki                    |
| `slug`     | URL-safe identifier (lowercase, hyphens, no spaces)  |
| `title`    | Human-readable title                                 |
| `content`  | Markdown body                                        |

The tool validates that the wiki and category are defined in `access.yml`, checks write access via `resolve_writable_wikis()`, then calls `write_bulletin()` which generates a unique slug (`blt-<uuid12>`), attaches metadata (source session, intended category, participants, timestamps), and writes the file into `memory/core/bulletins/`. The wiki index is rebuilt after each write.

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
1. Formats the memory prompts as bullet points.
2. Calls `write_bulletin()` to write a single bulletin containing all the prompts, with session metadata (session key, time window, participants, contact IDs).
3. The bulletin is placed in `memory/core/bulletins/` awaiting the dream process.

After all session summaries are processed, the heartbeat task runs `MemoryService.run_dream()` to curate pending bulletins into structured entries (see Dream Process below).

## The Dream Process

The dream process is the curation pipeline that transforms raw bulletins into well-structured, categorized memory entries. It runs automatically at the end of each heartbeat cycle (after session summaries are generated).

### How It Works

1. `run_dream()` reads all pending bulletins from `memory/core/bulletins/`.
2. If there are no bulletins, it returns immediately with `{"status": "empty"}`.
3. It builds a catalog of all bulletins with their metadata (session key, time window, participants, content).
4. It also builds a catalog of existing curated entries across all categories (`people`, `facts`, `events`, `locations`, `research`).
5. Both catalogs are sent to an LLM with a detailed system prompt that instructs it to:
   - CREATE new entries for new topics/people.
   - UPDATE existing entries by merging new information (newer info wins).
   - IGNORE bulletins with no factual claims.
6. The LLM returns a JSON array of write operations, each specifying `wiki`, `category`, `slug`, `title`, `content`, and `source_bulletins`.
7. Each operation is validated (wiki exists, category is valid, all fields non-empty, category is not `bulletins` or `digested`).
8. Valid operations are written via `write_entry()`, which creates the file and rebuilds the wiki index.
9. All processed bulletins are moved to `memory/core/digested/` for archival.
10. The result is logged to the `memory_dream_log` database table.

### Category Templates

The dream process uses category-specific templates to structure entries consistently:

- **people**: Overview, Personality, Interests, Dietary, Work, Family, Preferences, Contact, Relationships
- **events**: Summary, Date, Participants, Location, Details, Follow-up
- **facts**: Summary, Details, Procedures
- **locations**: Description, Address, Notes, Related
- **research**: Topic, Findings, Status, Notes

Templates can be overridden by placing a `_template.md` file in the category directory under `core/`. If no template file exists, the hardcoded defaults are used.

### Transcript References

The dream process includes `[[session:key window]]` tags on bullet points to trace information back to the conversation it came from. When updating an existing entry, existing tags are preserved and new ones are appended.

### Dream Log

Every dream run is logged to the `memory_dream_log` table with the number of bulletins processed, entries created, bulletin slugs, the full operations list, the raw LLM response, duration, and status. This powers the dashboard's dream log view.

### Linting

The `lint_entries()` method can restructure all curated entries to match the current category templates. It sends each entry to an LLM with the template instructions and rewrites it if the structure differs. This is available via the dashboard and ensures entries stay formatted consistently as templates evolve.

## Retrieval

### Lightweight Index (Always in Prompt)

The `prompt_assembler` module integrates memory during prompt construction in `load_workspace_prompt()`. The process:

1. `ensure_memory_structure()` is called to guarantee the directory exists.
2. The memory directory is checked for existence.
3. A `## Memory` section is appended to the system prompt, containing:
   - A description of the available tools (`memory_search`, `memory_read`, `memory_browse`, `memory_write`).
   - The wiki name and categories (currently hardcoded to `core` with `people`, `facts`, `research`).

This is a zero-overhead path -- no tool call, no extra latency. The assistant starts every turn knowing what it knows. Note that the `_index.md` content itself is loaded via `_build_memory_index_static()` which reads the index files for always-accessible wikis and prepends a header with tool usage instructions.

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

The search uses the model specified by `LLMDispatchService.memory_model` (configured via `openai.memory_model` in settings), with temperature 0.0 for deterministic results.

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
7. The reflection process writes bulletins for the prompts.
8. Prints a summary and the current memory index.

In dry-run mode, the LLM is not called -- the command just lists the prompts that would be processed. Note that seed only writes bulletins; the dream process must run separately (via the heartbeat or a dashboard trigger) to curate them into structured entries.

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

## Dream Logging

Every dream run is logged to the `memory_dream_log` table. The schema is defined in `schemas/301_memory_dream_log.sql` and extended by `302_memory_dream_log_raw_response.sql`:

```sql
CREATE TABLE IF NOT EXISTS memory_dream_log (
    id TEXT PRIMARY KEY,
    bulletins_processed INTEGER NOT NULL DEFAULT 0,
    entries_created INTEGER NOT NULL DEFAULT 0,
    bulletin_slugs TEXT NOT NULL DEFAULT '[]',
    operations_json TEXT NOT NULL DEFAULT '[]',
    raw_response TEXT,
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_dream_log_created
    ON memory_dream_log(created_at DESC);
```

The dream log is written by the heartbeat task in `SessionIdleSummaryTask.run()` after `run_dream()` completes. It captures the full result including the raw LLM response for debugging and auditing.

## Dashboard Memory Page

The dashboard includes a memory page at `/memory` with these features:

1. **Stats header** -- Total entry count and per-category counts, pulled from `GET /api/memory/stats`.
2. **Live search** -- An input field that calls `GET /api/memory/search?q=...` and displays results inline with abstract, relevance explanations, and latency. Search results are clickable to open a content viewer.
3. **Pending bulletins** -- Shows all bulletins in the queue with their source session, intended category, and content preview. Each bulletin is clickable to view full content.
4. **Dream log** -- A feed of dream runs showing status, bulletins consumed, entries created, duration, and the resulting operations. Each run is expandable to show:
   - The consumed bulletins with content (fetched from the `digested/` archive via `POST /api/memory/digested`).
   - The memory entries written (clickable to view content).
   - The raw LLM response for debugging.
   - A "re-digest" button to move a bulletin back from `digested/` to `bulletins/` for reprocessing.
5. **Lint** -- A button (with confirmation) to trigger `POST /api/memory/lint` which reformats all entries to match current category templates.
6. **Content viewer** -- An inline panel that loads any memory file's content via `GET /api/workspace/file?path=...`.

Dashboard API endpoints (defined in `routers/dashboard_api.py`):

| Endpoint | Description |
|----------|-------------|
| `GET /api/memory/stats` | Entry counts, per-category stats, pending bulletins, last dream time |
| `GET /api/memory/search?q=...` | Search the `core` wiki, log result, return with latency |
| `GET /api/memory/searches` | Last 100 search log entries with parsed results |
| `GET /api/memory/bulletins` | Current pending bulletins |
| `GET /api/memory/dreams` | Last 20 dream log entries |
| `GET /api/memory/category/{category}` | Entries in a specific category |
| `POST /api/memory/digested` | Fetch content of digested bulletins by slug |
| `POST /api/memory/redigest` | Move a digested bulletin back to the queue |
| `POST /api/memory/lint` | Reformat all entries to match templates |

All endpoints are protected by the dashboard secret (Bearer token or `?secret=` query parameter) if one is configured.

## Key Source Files

| File | Purpose |
|------|---------|
| `services/memory_service.py` | Core service: CRUD, index building, search, reflection, dream, lint, bulletins, config loading |
| `services/memory_tools.py` | LLM function-call tools (memory_write, memory_read, memory_search, memory_browse) |
| `services/prompt_assembler.py` | Injects memory index and tool descriptions into system prompt |
| `services/workspace_tools.py` | Guards `memory/` directory from direct write_file access |
| `services/session_summary_service.py` | Generates summaries with memory_prompts from session history |
| `heartbeat.py` | SessionIdleSummaryTask triggers reflection + dream after summaries are stored |
| `cli.py` | `cyborg memory seed` command for bulk processing historical summaries |
| `schemas/300_memory_search_log.sql` | Database schema for search logging |
| `schemas/301_memory_dream_log.sql` | Database schema for dream run logging |
| `schemas/302_memory_dream_log_raw_response.sql` | Adds raw_response column to dream log |
| `routers/dashboard_api.py` | Dashboard API endpoints for memory stats, search, bulletins, dreams, lint |
| `ui_app/src/routes/memory/index.tsx` | Dashboard memory page UI component |
