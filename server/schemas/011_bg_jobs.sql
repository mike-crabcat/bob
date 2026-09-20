-- bg_jobs (2026-09-20) — the DB store behind the bg_* tools and
-- run_bg_process, replacing workspace/.bg/processes.json. One row per
-- daemon/job run; terminal rows are the audit history (bg_status shows
-- alive by default). Wake jobs (wake_on_exit=1) carry the delivery ledger:
-- the dead systemd unit is the durable exit fact, `delivery` is the
-- wake-owed marker the watcher drains (pending → delivered). Jobs are
-- mechanics, not commitments — no task rows here; a job that genuinely
-- serves a tracked task links via tasks.expected_completer='bg-<name>.service'.
CREATE TABLE IF NOT EXISTS bg_jobs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,                       -- unit stem: bg-<name>.service
    unit TEXT,
    mechanism TEXT NOT NULL DEFAULT 'systemd',
    pid INTEGER,
    pid_start_time TEXT,
    command TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'bg_start',  -- bg_start | run_bg_process
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','exited','failed','timeout','killed',
                          'replaced','orphaned')),
    exit_code INTEGER,
    systemd_result TEXT,                      -- systemctl Result: success|exited|timeout|...
    wake_on_exit INTEGER NOT NULL DEFAULT 0,
    parent_session_key TEXT NOT NULL DEFAULT '',
    delivery TEXT NOT NULL DEFAULT ''         -- '' | pending | delivered (wake jobs only)
        CHECK (delivery IN ('','pending','delivered')),
    log TEXT NOT NULL DEFAULT '',             -- path under .bg/logs/
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bg_jobs_name ON bg_jobs(name);
CREATE INDEX IF NOT EXISTS idx_bg_jobs_status ON bg_jobs(status);
-- one running row per name: bg_start on a live name errors, on a dead
-- name marks the old row 'replaced' first
CREATE UNIQUE INDEX IF NOT EXISTS uq_bg_jobs_running_name
    ON bg_jobs(name) WHERE status = 'running';
