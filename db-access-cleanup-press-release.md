# Every Table Gets an Owner

*Press release — Bob engineering, August 2026*

**Bob is retiring scattered SQL. Starting now, every database table has a
named owner, and the test suite enforces it.**

## Why

Bob's database is its memory, its inbox, and its call log — one SQLite file
behind a live agent handling real WhatsApp, email, and voice traffic. Today,
69 files write their own SQL against it. The same lookup is reimplemented a
dozen ways; dashboard pages reach around services into core tables; and when
a table changes shape, we chase call sites through routers, heartbeat tasks,
CLIs, and tools by grep.

The recent session-model cleanup proved the cost is real: renaming one table
(`session_messages` → `messages`) required porting SQL in 20+ files, and the
work surfaced a latent pool deadlock caused by exactly this scatter — a
service acquiring a second connection mid-transaction because nothing owned
the query path.

## What changes

Two sanctioned homes for SQL, nothing else:

- **Repositories** for tables shared across domains (messages, contacts,
  conversations, phone calls, LLM call log…), exposing lifecycle methods —
  not generic CRUD — and transaction-aware by contract.
- **Domain stores** for single-domain tables (dreams, calendar, webhooks),
  where the owning service keeps its SQL and everyone else calls methods.

Routers, heartbeat, and CLI commands stop owning SQL entirely.

A new ownership test makes the rule structural: every table maps to its
owner set, writes are enforced strictly, and any new bypass fails the
pre-deploy gate the moment it's written — not months later during the next
migration.

## Why it's better

- **Schema changes become one-file changes.** The next rename touches the
  owner, not twenty call sites.
- **Bugs lose their hiding places.** Claim flags, atomic result dispatch,
  and lifecycle transitions live in one audited method each, instead of
  being re-derived inline wherever needed.
- **The deadlock class dies.** Transaction-aware repository APIs are
  mandatory, so no method silently grabs a second pool connection inside a
  caller's transaction.
- **It ratchets, never regresses.** The enforcement test's allowlist only
  shrinks. Six small, independently-deployed increments — safety harness
  first — each leave the system strictly better than the last.

The full plan lives in [db-access-cleanup-plan.md](./db-access-cleanup-plan.md).
