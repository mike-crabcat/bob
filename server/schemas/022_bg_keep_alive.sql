-- Keep-alive background daemons (2026-10-13). A reboot on 6 Oct unloaded
-- every transient bg unit; the watcher recorded the daemons as exited and
-- nothing restarted them — frigate-watchd (guard-group camera alerts) and
-- aus-legal stayed dead a week, blackline (crashed 24 Sep) three weeks.
-- keep_alive=1 jobs are restarted by the watcher when they die (crash or
-- reboot), capped per hour; past the cap the owner is alerted once.
-- bg_stop (status 'killed') is never restarted.
ALTER TABLE bg_jobs ADD COLUMN keep_alive INTEGER NOT NULL DEFAULT 0;
