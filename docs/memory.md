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

### access.yml

The `access.yml` file defines wikis, their categories, and access policies. If it does not exist when the system starts, a default is created automatically.

```yaml
wikis:
  core:
    description: "General knowledge"
    categories: [people, facts, events, locations, research]
    access: always
    write: always
  private:
    description: "Sensitive information"
    categories: [credentials, health, finances]
    access: trusted
    write: trusted
```

Each wiki has these fields:

- `description` -- human-readable label
- `categories` -- list of category names; each becomes a subdirectory
- `access` -- read access level: `always`, `trusted`, or `never`
- `write` -- write access level: `always`, `trusted`, or `never`

The config is cached in memory with mtime-based invalidation, so edits take effect on the next prompt assembly cycle.

### Per-Wiki Indexes (_index.md)

Each wiki has an auto-generated `_index.md` file that summarizes its contents in a compact format. The index is rebuilt every time an entry is written or updated.

Example `_index.md`:

```markdown
### core
**people**: alice-johnson (Alice Johnson, Software engineer at TechCorp), bob-smith (Bob Smith, Prefers dark roast coffee)
**facts**: coffee-preference (Coffee preference, Only drinks pour-over), timezone (Timezone, UTC+8 Australia/Perth)
**events**: trip-to-perth (Trip to Perth, Planned for March 2025)
```

The index for `always`-access wikis is loaded into every system prompt, so the assistant always knows what memory entries exist. This is intentionally lightweight -- just slugs, titles, and truncated summaries. Full content is retrieved on demand via `memory_read`.

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

The tool validates that the wiki and category are defined in `access.yml`, checks write access, writes the file, and rebuilds the wiki index. The workspace's `write_file` tool is guarded to reject writes into `memory/` -- all modifications must go through `memory_write` to keep indexes consistent.

### Post-Session: Reflection via Heartbeat

After a conversation goes idle, the heartbeat system generates a summary and extracts `memory_prompts` -- a list of facts worth remembering. The `SessionIdleSummaryTask` (in `heartbeat.py`) then calls `MemoryService.reflect_and_update()`, which:

1. Resolves which wikis are writable for the session.
2. Builds the current memory index so the LLM can see what already exists.
3. Sends the summary and memory prompts to an LLM with instructions to produce JSON write operations.
4. Validates each operation (wiki exists, category is valid, all fields present).
5. Calls `write_entry()` for each valid operation.

This means memory grows organically from conversation content without requiring explicit user action. The LLM decides what is genuinely useful and avoids duplicating existing entries.

## Retrieval

### Lightweight Index (Always in Prompt)

The `prompt_assembler` module calls `MemoryService._build_memory_index_static()` during prompt construction. It reads `_index.md` from each `always`-access wiki and appends the result under a `## Memory` heading in the system prompt. The index header instructs the assistant to use `memory_read` for full details and `memory_search` to find entries.

This is a zero-overhead path -- no tool call, no extra latency. The assistant starts every turn knowing what it knows.

### memory_read Tool

Reads a single entry by wiki, category, and slug. Returns the full markdown content (title heading + body). Checks that the session has read access to the requested wiki.

### memory_search Tool

Semantic search across one or all accessible wikis. The search is LLM-powered:

1. Collects all entries (excluding `_index.md` files) from the target wikis.
2. Builds a catalog with full text (truncated to 500 chars per entry) and workspace-relative paths.
3. Sends the catalog and query to an LLM with a strict system prompt requesting a JSON response with an abstract and result list.
4. Falls back to keyword matching if the LLM response is not valid JSON.

Returns `{abstract, results}` where each result has `path`, `title`, and `relevance` (a sentence explaining why it matched). Results use workspace-relative paths like `memory/core/people/alice-johnson.md` so the assistant can use `read_file` to get the full document.

### memory_browse Tool

Lists all entries in a wiki category. Returns an array of `{slug, title, modified}` sorted alphabetically. Useful for exploring what exists in a category before searching.

## Access Control

Each wiki has independent read (`access`) and write (`write`) policies:

| Level    | Behavior                                                              |
|----------|-----------------------------------------------------------------------|
| `always` | Accessible to all sessions, including unauthenticated ones            |
| `trusted`| Only accessible when `session_participants.is_trusted = 1` for the session |
| `never`  | Never accessible (not currently used, reserved for future use)        |

The trust check queries the `session_participants` table:

```sql
SELECT 1 AS ok FROM session_participants
WHERE session_key = ? AND is_trusted = 1 LIMIT 1
```

Access is resolved per-request in `MemoryService.resolve_accessible_wikis()` and `resolve_writable_wikis()`. The `memory_write`, `memory_read`, `memory_search`, and `memory_browse` tools all check access before performing any operation.

## CLI: cyborg memory seed

The `cyborg memory seed` command bulk-processes historical session summaries to populate the memory wiki retroactively. This is useful when the memory system is first enabled on an existing installation.

```bash
# Dry run -- see what would be processed
cyborg memory seed --dry-run

# Process in batches of 10 summaries per LLM call
cyborg memory seed --batch-size 10
```

The command:

1. Queries `session_summaries` for rows with non-empty `memory_prompts`.
2. Groups summaries into batches (by insertion order, configurable size).
3. For each batch, combines summaries and memory prompts, then calls `reflect_and_update()` with a synthetic `bulk_seed` session key.
4. The reflection LLM decides which facts to write, just like the real-time path.
5. Prints a summary and the current memory index.

## Search Logging

Every `memory_search` call (from tool or dashboard) is logged to the `memory_search_log` table:

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

| Column           | Description                                          |
|------------------|------------------------------------------------------|
| `id`             | UUID primary key                                     |
| `query`          | The search query string                              |
| `results_json`   | Full JSON response (abstract + results array)        |
| `session_key`    | Session that initiated the search (null for dashboard) |
| `result_count`   | Number of results returned                           |
| `latency_seconds`| Wall-clock time for the search operation             |
| `created_at`     | Timestamp                                           |

This table powers the dashboard memory page's history view and is useful for understanding what the assistant searches for and how effective retrieval is.

## Dashboard Memory Page

The dashboard includes a memory page at `/memory` with two features:

1. **Live search** -- An input field that calls `/api/memory/search?q=...` and displays results inline with abstract, relevance explanations, and latency.
2. **Search history** -- Fetches the last 100 entries from `/api/memory/searches` and displays them as an expandable list showing query, result count, latency, and relative time.

Dashboard API endpoints:

- `GET /api/memory/searches` -- Returns the last 100 rows from `memory_search_log` with parsed results.
- `GET /api/memory/search?q=...` -- Runs a memory search against the `core` wiki (dashboard always searches `core`) and logs the result.

## Key Source Files

| File | Purpose |
|------|---------|
| `services/memory_service.py` | Core service: CRUD, index building, search, reflection |
| `services/memory_tools.py` | LLM function-call tools (memory_write, memory_read, memory_search, memory_browse) |
| `services/prompt_assembler.py` | Injects memory index into system prompt |
| `services/workspace_tools.py` | Guards `memory/` directory from direct write_file access |
| `heartbeat.py` | SessionIdleSummaryTask triggers reflection after summaries |
| `cli.py` | `cyborg memory seed` command |
| `schemas/300_memory_search_log.sql` | Database schema for search logging |
| `routers/dashboard_api.py` | Dashboard API endpoints for memory search |
| `ui_app/src/routes/memory/index.tsx` | Dashboard memory page UI component |
