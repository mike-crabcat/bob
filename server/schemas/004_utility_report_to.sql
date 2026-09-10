-- report_to (docs/utility-conversations-plan.md Part 3): where a utility
-- conversation's concerning output goes. When set, the utility wake exposes
-- a send_report tool; delivery is a wake of the target conversation — the
-- target session's own turn receives the report and decides how to raise it
-- (keeps its triage role; no per-message approval round-trip, because the
-- sensation route itself was owner-approved — "the approval IS the safety
-- review"). NULL = watch-only: output stays in this conversation's history.
ALTER TABLE utility_conversations ADD COLUMN report_to TEXT;
