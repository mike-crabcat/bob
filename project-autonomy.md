# Project Autonomy Plan

## Goal

Make project execution autonomous enough that:

1. When a task completes, any tasks that are now dependency-unblocked are moved into the right actionable state and immediately worked.
2. When a project has no remaining incomplete tasks, Cyborg evaluates the project's success criteria.
3. If success criteria are met, Cyborg generates a conclusion, closes the project, and announces completion.
4. If success criteria are not met, Cyborg raises the next set of tasks needed to satisfy them and resumes execution.

The behavior should be deterministic, idempotent, and consistent with the existing task planning rules:

- tasks must begin in `planning`
- tasks must not become `pending` until a plan is approved
- tasks blocked by dependencies must not be notified or assigned until the dependency is cleared

## Current State

The current code has pieces of this, but not the full autonomy loop:

- [cyborg/services/task_service.py](./cyborg/services/task_service.py)
  - `complete_task()` triggers notifications, journals, and `ProjectExecutionService.on_task_completed(...)`.
  - `unblock_task()` exists, but it is only invoked manually.
  - status transitions do not currently account for dependency-blocked tasks.
- [cyborg/services/plan_service.py](./cyborg/services/plan_service.py)
  - `approve_plan()` always moves a task to `pending`, even if its dependency is not complete.
- [cyborg/services/project_execution_service.py](./cyborg/services/project_execution_service.py)
  - progression is step-order based, not dependency-aware
  - `_get_current_step_index()` assumes completed task count equals current step
  - `_is_step_satisfied()` is effectively permissive and does not govern real project autonomy
  - success criteria can auto-close a project, but only after the step heuristic decides all steps are done
- [cyborg/services/notification_service.py](./cyborg/services/notification_service.py)
  - `_task_needs_input()` suppresses notifications for child tasks whose `parent_id` is incomplete
  - this is only notification gating; it does not actually release tasks when dependencies clear
- [cyborg/models.py](./cyborg/models.py)
  - tasks support only a single dependency via `parent_id`
  - projects have `metadata` on the response model, but `ProjectCreate` and `ProjectUpdate` do not expose metadata today

## Desired Runtime Behavior

### 1. Dependency-aware task readiness

Use `parent_id` as the dependency signal in v1.

- A task with an incomplete parent is dependency-blocked.
- Dependency-blocked tasks must not move to `pending` or `active`.
- If a dependency-blocked task has no approved plan, it remains `planning`.
- If a dependency-blocked task does have an approved plan, it should be represented as `blocked` with a standardized dependency-blocked reason.
- When the parent task completes successfully, dependent tasks should be re-evaluated immediately.

### 2. "Worked on" means actionable dispatch, not just visible presence

When a task becomes unblocked:

- if it has an approved current plan, transition it to `pending`
- if it has a target session, the existing assignment notification flow should dispatch it immediately
- if it does not have a target session, the source-side needs-input flow should make it visible to the source session
- if it still lacks an approved plan, keep it in `planning` and create the appropriate planning prompt

### 3. End-of-project autonomy

When a task completes, and that completion leaves a linked project with no incomplete tasks, Cyborg should:

1. evaluate project success criteria
2. if all criteria are met:
   - generate the project conclusion
   - close the project
   - write a journal entry
   - notify the source session that the project is complete
3. if criteria are not met:
   - generate additional tasks required to satisfy the unmet criteria
   - link them to the project
   - give each new task an initial plan
   - immediately work any newly actionable tasks

### 4. Idempotency

The same completion event must not:

- create duplicate follow-up tasks
- generate multiple conclusions
- send multiple completion announcements

Repeated calls, retries, and service restarts must be safe.

## Recommended Design

## A. Add a dedicated autonomy orchestrator

Introduce a dedicated service, for example:

- `cyborg/services/project_autonomy_service.py`

Responsibilities:

- handle post-task-completion orchestration
- release dependency-blocked child tasks
- evaluate whether linked projects have reached an autonomy checkpoint
- either generate follow-up tasks or complete the project
- centralize idempotency checks and journaling

This is a better fit than continuing to overload `ProjectExecutionService`, which is currently centered on a linear plan-step heuristic.

Suggested entrypoints:

- `on_task_completed(task_id: str, result_summary: str | None) -> None`
- `_release_unblocked_dependents(completed_task_id: str) -> list[str]`
- `_handle_project_after_task_completion(project_id: str, trigger_task_id: str) -> None`
- `_project_has_incomplete_tasks(project_id: str) -> bool`
- `_evaluate_project_success(project_id: str) -> SuccessEvaluation`
- `_generate_follow_up_tasks(project_id: str, evaluation: SuccessEvaluation) -> list[str]`
- `_complete_project(project_id: str, evaluation: SuccessEvaluation) -> None`

## B. Make dependency readiness a first-class rule

Implement a shared helper in task/plan logic:

- `_dependency_is_satisfied(task_row) -> bool`

Rules:

- no `parent_id` -> dependency satisfied
- parent exists and is `completed` -> dependency satisfied
- otherwise -> dependency blocked

Use this helper in:

- `TaskService.create_task()`
- `TaskService.update_task()`
- `PlanService.approve_plan()`
- the new autonomy orchestrator

### Status rules

- `planning`
  - no approved plan yet
- `blocked`
  - approved plan exists, but dependency is incomplete
- `pending`
  - approved plan exists and dependency is satisfied
- `active`
  - work has begun

This preserves the existing planning invariant and gives a clean place to hold approved-but-not-yet-runnable tasks.

## C. Release dependents on task completion

On successful task completion:

1. find child tasks where `parent_id = completed_task_id`
2. for each non-deleted child task:
   - if it has an approved current plan, move it to `pending`
   - otherwise leave it in `planning`
   - clear standardized dependency-block fields if present
   - add task history entry such as `dependency_released`
3. call notification sync immediately so newly unblocked tasks are actually worked

This should happen before project-level evaluation, so downstream work can begin as soon as possible.

## D. Replace "step count == progress" with "open task set == progress"

Current project auto-execution assumes:

- completed task count determines plan progress
- a project reaches completion once all plan steps appear done

That is too weak for autonomous execution once tasks can fan out or be generated dynamically.

Replace the checkpoint rule with:

- a project reaches evaluation when it has no remaining incomplete tasks in:
  - `planning`
  - `blocked`
  - `pending`
  - `active`

Failed tasks should not silently count as success. They should be included in evaluation context so the autonomy logic can either:

- create remediation tasks
- or leave the project uncompleted if criteria are still unmet

## E. Evaluate success criteria against project state, task results, and journal history

Keep the existing `SuccessCriterion` structure, but strengthen the evaluation context:

- project metadata and state
- project aim, method, plan, and success criteria
- all linked task titles, statuses, plans, and results
- journal milestones and blockers
- counts of total, completed, failed, blocked tasks

Output of evaluation should be structured, for example:

- `all_met: bool`
- `met_criteria: list[...]`
- `unmet_criteria: list[...]`
- `reasoning_summary: str`
- `recommended_tasks: list[TaskDraft]`
- `conclusion: str | None`

## F. When criteria are unmet, generate follow-up tasks instead of stalling

Once a project has no incomplete tasks but is not yet successful, Cyborg should create the next tasks automatically.

Task generation should use:

- project aim
- project method
- success criteria descriptions
- unmet criteria
- recent task results
- recent journal history

Every generated task must include:

- `title`
- `description`
- `plan`
- `priority`
- `project_ids`
- dependency metadata if needed

Recommended metadata on generated tasks:

- `auto_created_by_project: true`
- `autonomy_cycle: <int>`
- `autonomy_trigger_task_id: <uuid>`
- `autonomy_reason: "unmet_success_criteria"`

If follow-up tasks should preserve routing, inherit the project's source metadata and, when appropriate, a project-level target routing template.

## G. Add project-completion notifications

There is currently `task_result` but no dedicated project completion notification type.

Add:

- `project_result` or `project_completed`

It should:

- target the project's source session
- include the project title
- include the generated conclusion or a concise summary
- be created exactly once per project closure

This is the mechanism that will "announce the project as complete".

## H. Expose project metadata on create/update

Project completion announcements depend on project source routing metadata, but project create/update models do not currently expose metadata even though the DB and response model support it.

Add `metadata` to:

- `ProjectCreate`
- `ProjectUpdate`
- project CLI create/update commands

This will let projects carry the same routing context that tasks already use.

## I. Record autonomy decisions for replay safety

Use one of these patterns:

### Option 1: lightweight, no new table

Record autonomy actions in project journal metadata:

- `autonomy_action`
- `trigger_task_id`
- `autonomy_cycle`
- `generated_task_ids`

Before generating follow-up tasks or closing a project, check whether the same action has already been recorded for the same trigger.

### Option 2: stronger, recommended if this grows

Add a dedicated `project_autonomy_runs` table with uniqueness on:

- `project_id`
- `trigger_task_id`
- `action_type`

This is the cleaner long-term choice if project autonomy becomes a core workflow.

## Implementation Phases

## Phase 1: dependency release and readiness

Changes:

- add dependency readiness helper
- update plan approval to respect dependencies
- standardize dependency-blocked state
- release child tasks when parent completes
- sync notifications immediately for released tasks

Files:

- `cyborg/services/task_service.py`
- `cyborg/services/plan_service.py`
- `cyborg/services/notification_service.py`
- `cyborg/models.py`

Success condition:

- approved child tasks no longer go `pending` while their parent is incomplete
- when parent completes, released children become actionable automatically

## Phase 2: project autonomy checkpoint

Changes:

- add autonomy orchestrator service
- move post-completion project logic into it
- replace step-count progression with open-task-set checkpointing
- evaluate criteria when the project has no remaining incomplete tasks

Files:

- `cyborg/services/project_autonomy_service.py`
- `cyborg/services/task_service.py`
- `cyborg/services/project_execution_service.py`

Success condition:

- completing the last incomplete task of an auto-executing project always triggers one of:
  - project closure
  - follow-up task generation

## Phase 3: follow-up task synthesis

Changes:

- generate concrete tasks from unmet criteria
- persist autonomy-cycle metadata
- link generated tasks to the project
- immediately dispatch any generated task that is already actionable

Files:

- `cyborg/services/project_autonomy_service.py`
- `cyborg/models.py`
- possibly prompt/template assets if task drafting uses OpenClaw or an LLM helper

Success condition:

- projects that are not yet successful do not stall after the last current task finishes

## Phase 4: project completion announcement

Changes:

- add project completion notification type
- generate and dispatch completion announcements
- ensure project metadata can carry source routing

Files:

- `cyborg/models.py`
- `cyborg/services/notification_service.py`
- `cyborg/services/openclaw_hook_service.py`
- `cyborg/services/project_service.py`
- `cyborg/cli.py`

Success condition:

- source session receives a clear project completion announcement with conclusion text

## Test Plan

### Dependency release

- creating a child task with `parent_id` and approving its plan while the parent is incomplete leaves it dependency-blocked, not `pending`
- completing the parent releases the child to `pending`
- releasing a child triggers task assignment if `target_session` exists
- releasing a child without an approved plan leaves it in `planning`
- failed parent task does not release child tasks

### Project autonomy

- auto-executing project with no remaining incomplete tasks and met criteria closes automatically
- auto-executing project with no remaining incomplete tasks and unmet criteria creates follow-up tasks
- follow-up tasks are linked to the project and contain initial plans
- repeated completion calls do not duplicate generated tasks
- repeated evaluation does not create duplicate completion announcements
- non-auto-executing projects keep existing manual behavior

### Notification and routing

- dependency-blocked tasks do not emit assignment notifications
- newly released tasks emit assignment notifications immediately
- project completion notifications route to the project source session
- follow-up tasks inherit the expected routing metadata

## Risks and Open Questions

1. `parent_id` only supports a single dependency.
   If projects need true DAG workflows, add a `task_dependencies` table later.

2. Follow-up task generation from unmet criteria is not purely mechanical.
   A deterministic fallback may be possible for simple criteria, but richer projects will likely need an LLM-assisted draft step.

3. Project routing is currently under-modeled.
   Without `ProjectCreate`/`ProjectUpdate` metadata support, announcing completion is unreliable.

4. Silent exception swallowing should be reduced.
   The current `TaskService._trigger_project_execution()` path swallows failures, which makes autonomy bugs hard to diagnose.

## Recommended First Slice

Implement Phases 1 and 2 first.

That gives the core behavioral win:

- tasks release automatically when dependencies clear
- completing the final incomplete task of a project causes a real project-level decision

Then add Phase 3 and Phase 4 so the project can fully continue or conclude without manual intervention.
