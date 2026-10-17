# Coder: a native coding subagent (plan)

**Status:** plan, 2026-10-18. Temporary — implement, fold the outcome into a
reference doc (`docs/datamodel.md` + a section in `AGENTS.md`), delete this
file.

## Why

Coding work today goes to the Claude Code CLI
(`subagent_service._run_claude`: `claude -p --output-format json
--allowed-tools "Read Write Glob Grep Edit Bash"`). That has three problems:

1. **No conversation while it runs.** Headless `-p` is one shot: brief in,
   JSON out. Bob can't steer it, and it can't ask Bob anything — so a lossy
   brief means a guessed result.
2. **All-or-nothing.** A run is capped at 1800 s
   (`BOB_HARNESS_SKILL_DEV_TIMEOUT_SECONDS`); on timeout it is killed and
   everything it learned is thrown away (2026-10-17 house-listings run). A
   deploy restart kills it the same way (rc 143).
3. **External dependency, unsandboxed.** The Docker image installs
   `@anthropic-ai/claude-code`; the CLI runs as the bob user outside the
   Landlock workspace sandbox.

The retired `local` type (2026-10-06) was not a fix: Bob's model with
workspace tools and nothing else — no loop design, no messaging, no
role — and Bob believed it had his memory when it didn't.

**Objective:** a coder that runs on Bob's own harness, performs on par with
Claude Code / Codex / Copilot's coding agent on the work Bob actually
delegates, talks both ways with Bob while it runs, and needs no Claude CLI.

## Design

### Shape: one continuous run, not rounds

Mainstream coding agents run one long agent loop held up by a plan/todo
tool, context compaction and budget backstops — not scheduled rounds.
Goal-room rounds exist because goals wait on time and people; a coding
task doesn't. So:

- **Worker conversation** `agent:coder-<id>` per spawn (its own history,
  traces, dashboard timeline), parent = the spawning conversation.
- **One continuous `run_turn`** with a high iteration cap. The task
  continues until the model calls `finish`, asks and waits (`ask_bob`), or
  hits a budget.
- **Plan tool** (`update_plan`): a checklist the model maintains; persisted
  as `PLAN.md` in the task dir and mirrored to the parent's Work block.
- **Compaction instead of rounds.** Near the model's context limit:
  summarise the work so far into `NOTES.md`, keep the recent tail, continue
  the same run. Tool-loop folding (`tool_loop_folding.py`) is the first
  half of this already.
- **Durable checkpoints.** Plan, notes and transcript are flushed as the
  run goes. After a restart the run **resumes from its checkpoint**
  (boot re-arm for `agent:coder-*`), it is not killed. Bob restarts on
  every deploy; CLI agents mostly don't have this problem, we do.
- **Budgets are backstops,** not a rhythm: total cost, wall clock and
  iterations per task, set at spawn with sane defaults. Hitting one ends in
  a wrap-up (summary + state left on disk, resumable), never a silent kill.

### Tools

Workspace — already exist (eval-proven: `edit_large_file_surgical`,
`rename_function_multi_site`, `edit_quote_heavy_block` 9/9 on Luna and GLM,
2026-10-12):

- `read_file(path, offset, limit)` — paged, line-numbered.
- `edit_file(path, old_string, new_string, replace_all)` — exact-text
  replacement, unique match, returns the diff (the `str_replace` tool).
- `multi_edit(path, edits[])` — several replacements to one file,
  all-or-nothing.
- `bash` — Landlock-sandboxed; `run_bg_process` for long commands (woken on
  exit); `read_image` to check renders; web search/fetch; browser skill.

New for parity (P1):

- **`apply_patch`** — Codex's multi-file, multi-hunk format, including file
  create/delete/rename, atomic. GPT-family models are trained on it; this
  is the biggest single tool gap.
- **`write_file`** — create/overwrite with the result echoed (today: bash
  heredocs, which break on quotes and aren't diffed).
- **`grep` / `glob`** — structured, capped output instead of raw `rg`.
- **Stale-edit guard** — refuse an edit if the file changed since this
  coder last read it (Bob and a coder, or two coders, can share files).
- **Checkpoint / undo** — snapshot touched files per task (git in the task
  work area, or copies) so a bad run rolls back and the parent gets one
  clean diff.
- Later: **diagnostics** — run pyright/ruff/tests after edits and feed
  errors back automatically.

Integration:

- `ask_bob(question, kind)` — see below.
- `report_progress(text)` — to the parent's Work block + dashboard.
- `finish(result, artifacts[])` — the only way a run completes; artifacts
  (files, media) are handed to the parent, which delivers them.
- `read_parent_history(limit, query)` — pull the parent conversation's
  recent dialogue on demand (fable-todo #6, pull variant).
- `docs_search`.

### Talking both ways

- **Bob → coder:** `message_subagent` lands in the coder **at the next tool
  boundary**, mid-run — the same injection mechanism as Batch 3
  (mid-turn human messages into a running conversation turn). Built once,
  used by both.
- **Coder → Bob: `ask_bob(question, kind)`**, `kind` ∈ `clarify` |
  `plan_signoff` | `approval`. The coder parks (durable — survives
  restarts); Bob is woken with the question in context; his answer resumes
  the run.
  - `clarify` — Bob answers from history/memory if he can, else asks the
    requester. The coder never gets memory itself; Bob is the bridge.
  - `plan_signoff` — Bob checks the plan against the request; approves
    small/clearly-scoped plans himself; code/skill/config changes and
    expensive work go to the human (existing propose-first rule).
  - `approval` — irreversible or paid steps (deletes, external sends,
    spend) **always** go to a human (goal-room approval rule).
- **Sign-off level is set per spawn,** not left to the coder:
  `signoff: none | plan | plan+irreversible` (quick script → `none`; skill
  rewrite → `plan`; repo or money → `plan+irreversible`).
- **Anti-chatter:** the tool says decide anything reversible yourself, ask
  only when the answer changes what gets built; a per-task question cap,
  then "say why"; every Q&A logged on the task.
- **No answer in time:** take the safest documented option or park the
  task as `waiting` — never guess on an `approval`.

### Sandbox and scope

- bash stays under Landlock (`BOB_BASH_SANDBOX=landlock`).
- Writes limited to the task dir, `scratch/`, and dirs granted at spawn.
- **Bob's own repo is off-limits unless granted per spawn** (decision 3).
- Child env is allowlist-only (no secrets), as for MCP stdio servers.
- Kill switches: `BOB_CODER=off` (routing back to Claude while it exists),
  per-task kill via `kill_subagent`.

### Model

Model-agnostic: a `coder` alias in `models.yaml`. P0/P1 evals pick it
(Luna vs GLM vs others). Expect a GPT-family model with `apply_patch` plus
`edit_file` fallback to lead, but the eval decides.

## Phases

| Phase | Scope | Gate to move on |
|---|---|---|
| **P0 — measure** (S) | Classify the last ~60 Claude briefs (code edits, scraping, PDF/doc builds, media pipelines, research). Build 10–15 replayable eval tasks with checkable outcomes from them, **plus test-verified coding tasks** (bug fix / feature / multi-file refactor on a small repo and a throwaway `bob` checkout, pass only by running tests). Baseline Claude Code on all of it. | Baseline recorded |
| **P1 — core coder** (M) | Worker conversation, continuous loop, plan tool, compaction, checkpoints + boot resume, budgets with wrap-up, `finish`, new tools (`apply_patch`, `write_file`, grep/glob, stale guard, undo). `agent_type="coder"` behind a flag; Claude unchanged. | Parity with Claude on P0 (per category, CIs), no sandbox escapes |
| **P2 — talking both ways** (M) | `ask_bob` (3 kinds, sign-off levels, timeouts, cap), mid-run injection (shared with Batch 3), `report_progress` → Work block + dashboard. | Evals: asks when it should, doesn't when it shouldn't; steering lands mid-run |
| **P3 — integration** (S–M) | `read_parent_history`, artifact handoff to the parent chat, dashboard coder view; optional read-only memory lookup (decision 2). | Live burn-in on real delegations |
| **P4 — cutover** (S) | `coder` default; spawn routing/docstrings switch; Claude behind a fallback flag for burn-in, then remove the CLI from the Dockerfile and the Claude code paths. Fold outcome into reference docs, delete this plan. | A burn-in period without needing the fallback |

## Decisions

1. **Loop machinery:** reuse only the goal-room *pieces* that fit (budget
   tracking, report-back, boot re-arm, traces) — not the round loop.
   *(Recommended; settled by the "one continuous run" design above.)*
2. **Memory access:** none — Bob answers memory/people questions via
   `ask_bob`. *Decided 2026-10-18 (Mike); revisit in P3.*
3. **Repo write access:** off by default, granted per spawn. *Open —
   recommended.*
4. **First cut:** P0 + P1, judge parity, then P2 — or P1 + P2 together
   since mid-run injection is shared with Batch 3? *Open.*

## Risks

- **Parity is a model question as much as a tool question.** If no
  available model matches Claude Code on the test-verified tasks, the
  coder ships as an option, not a replacement.
- **Compaction loses detail.** Mitigate with `PLAN.md`/`NOTES.md` as the
  durable memory of the run and a "re-read your notes" step after each
  compaction.
- **Over-asking.** `ask_bob` can turn a coder into a pager; the cap, the
  per-spawn sign-off level and the Q&A log are there to catch it.
- **Two workers, one file.** Stale-edit guard + per-task write scopes.
- **Cost.** Long runs on a frontier model are not cheap; per-task budgets
  and the eval's cost column decide the default model.

## Metrics to watch after cutover

Task completion rate vs the Claude baseline, wall-clock per task, cost per
task, `ask_bob` count per task and how many reached a human, budget
wrap-ups, resumes after restart, and parent-side follow-ups ("that's not
what I asked") as a proxy for brief quality.
