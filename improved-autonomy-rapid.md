# Rapid Autonomy Implementation - 1 Day Plan

**Goal:** Get basic autonomous project execution running by tomorrow.

**Trade-offs:** Faster delivery, more risk, technical debt accepted.

---

## What We're Building (MVP)

**Core capability:** Projects can auto-execute → evaluate criteria → complete or generate follow-up tasks

**What works:**
- OpenClaw evaluates success criteria (not regex)
- OpenClaw generates follow-up tasks for unmet criteria
- OpenClaw refines strategy when tasks fail
- Projects auto-complete when criteria met

**What's deferred:**
- Sophisticated context building (use simple dumps)
- Learning service (insights come later)
- Health monitoring (add later)
- Elegant error handling (make it work first)
- Comprehensive tests (manual testing only)
- Multiple scopes (use one size fits all)
- Database migrations (add columns manually if needed)

---

## Pre-req Checklist

Before starting, confirm:

- [ ] OpenClaw gateway is accessible from Cyborg
- [ ] Have a test project with tasks and success criteria
- [ ] Can manually call `openclaw gateway call` from CLI
- [ ] Cyborg service is running and accessible
- [ ] Have `uv run cyborg` working locally

---

## Implementation (Do in Order)

### Step 1: Context Builder (1 hour)

**File:** `cyborg/services/context_builder.py`

```python
"""Simple context builder - MVP version."""

from typing import Any
from cyborg.services.base import BaseService

class ContextBuilder(BaseService):
    """Build project context for OpenClaw reasoning."""

    async def build_project_context(self, project_id: str) -> dict[str, Any]:
        """Build context - simple dump, no filtering yet."""

        # Get project
        project = await self.db.fetch_one(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,)
        )

        # Get tasks
        tasks = await self.db.fetch_all(
            """
            SELECT t.* FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ?
            """,
            (project_id,)
        )

        # Get journal
        journal = await self.db.fetch_all(
            "SELECT * FROM project_journal_entries WHERE project_id = ? ORDER BY created_at DESC LIMIT 20",
            (project_id,)
        )

        return {
            "project": dict(project),
            "tasks": [dict(t) for t in tasks],
            "journal": [dict(j) for j in journal],
        }
```

**Done when:** Can call `context_builder.build_project_context(project_id)` and get a dict back.

---

### Step 2: OpenClaw Reasoning Service (2 hours)

**File:** `cyborg/services/openclaw_reasoning_service.py`

```python
"""OpenClaw reasoning service - MVP version."""

from uuid import uuid4
from cyborg.services.base import BaseService
from cyborg.services.context_builder import ContextBuilder
from cyborg.services.openclaw_hook_service import OpenClawHookService

class OpenClawReasoningService(BaseService):
    """LLM reasoning through OpenClaw gateway."""

    def __init__(self, db: Database):
        super().__init__(db)
        self.context_builder = ContextBuilder(db)
        self.openclaw = OpenClawHookService(db)

    async def evaluate_success_criteria(self, project_id: str) -> dict[str, Any]:
        """Ask OpenClaw to evaluate project success."""

        # Build context
        context = await self.context_builder.build_project_context(project_id)

        # Build prompt
        prompt = self._build_evaluation_prompt(context)

        # Call OpenClaw
        response = await self._call_openclaw(prompt)

        # Parse JSON response
        import json
        return json.loads(response)

    async def generate_follow_up_tasks(
        self,
        project_id: str,
        unmet_criteria: list[str],
    ) -> list[dict[str, Any]]:
        """Generate tasks for unmet criteria."""

        context = await self.context_builder.build_project_context(project_id)

        prompt = self._build_followup_prompt(context, unmet_criteria)
        response = await self._call_openclaw(prompt)

        import json
        return json.loads(response).get("tasks", [])

    async def _call_openclaw(self, prompt: str, timeout: int = 45) -> str:
        """Call OpenClaw gateway agent method."""

        params = {
            "message": prompt + "\n\nRespond with valid JSON only.",
            "deliver": False,
            "sessionKey": "cyborg:reasoning",
            "thinking": "verbose",
            "timeout": timeout * 1000,
            "idempotencyKey": str(uuid4()),
        }

        response = await self.openclaw._send_gateway_request(
            method="agent",
            params=params,
            expect_final=True,
            timeout_seconds=timeout,
        )

        # Extract content from response
        if isinstance(response, dict):
            return response.get("content", response.get("text", str(response)))
        return str(response)

    def _build_evaluation_prompt(self, context: dict[str, Any]) -> str:
        """Build prompt for success criteria evaluation."""

        p = context["project"]

        # Parse criteria
        import json
        try:
            criteria = json.loads(p.get("success_criteria", "[]"))
        except:
            criteria = []

        criteria_text = "\n".join([
            f"  {i+1}. {c.get('description', '')} (check: {c.get('check', '')})"
            for i, c in enumerate(criteria)
        ])

        tasks_summary = self._summarize_tasks(context["tasks"])

        return f"""Evaluate whether this project has achieved its success criteria.

Project: {p.get('title')}
Aim: {p.get('aim')}

Success Criteria:
{criteria_text}

Current State:
{tasks_summary}

Recent Journal:
{self._format_journal(context['journal'][:5])}

Respond with valid JSON:
{{
  "all_met": true/false,
  "met_criteria": ["criterion 1", "criterion 2"],
  "unmet_criteria": ["criterion 3"],
  "reasoning": "Brief explanation..."
}}
"""

    def _build_followup_prompt(self, context: dict[str, Any], unmet: list[str]) -> str:
        """Build prompt for follow-up task generation."""

        p = context["project"]

        return f"""Generate a task to address unmet success criteria.

Project: {p.get('title')}
Aim: {p.get('aim')}

Unmet Criteria:
{chr(10).join(f'  - {c}' for c in unmet)}

Generate ONE concrete task to satisfy these criteria.

Respond with valid JSON:
{{
  "title": "Task title",
  "description": "Full description",
  "plan": "Step-by-step plan",
  "priority": "high"
}}
"""

    def _summarize_tasks(self, tasks: list[dict]) -> str:
        """Simple task summary."""
        summary = {
            "total": len(tasks),
            "completed": len([t for t in tasks if t["status"] == "completed"]),
            "failed": len([t for t in tasks if t["status"] == "failed"]),
            "active": len([t for t in tasks if t["status"] == "active"]),
        }
        return f"Tasks: {summary['total']} total, {summary['completed']} completed, {summary['failed']} failed, {summary['active']} active"

    def _format_journal(self, entries: list[dict]) -> str:
        """Format journal entries."""
        return "\n".join([
            f"  - [{e['entry_type']}] {e['content'][:100]}..."
            for e in entries
        ])
```

**Done when:** Can call `evaluate_success_criteria(project_id)` and get back a dict with `all_met`.

---

### Step 3: Update Project Autonomy Service (1 hour)

**File:** `cyborg/services/project_autonomy_service.py`

**Find the `evaluate_and_complete` method and replace:**

```python
async def evaluate_and_complete(self, project_id: str) -> ProjectResponse | None:
    """Evaluate success criteria using OpenClaw and auto-complete or generate follow-ups."""

    from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService

    project = await self._get_project_row(project_id)
    if not project or project["state"] != ProjectState.ACTIVE.value:
        return None

    if await self._project_has_open_tasks(project_id):
        return None

    # Use OpenClaw for evaluation
    reasoning = OpenClawReasoningService(self.db)
    evaluation = await reasoning.evaluate_success_criteria(project_id)

    if evaluation.get("all_met"):
        # Generate conclusion and complete
        conclusion = await self._generate_conclusion_from_evaluation(project_id, evaluation)

        now = utcnow().isoformat()
        await self.db.execute(
            "UPDATE projects SET state = ?, closed_at = ?, conclusion = ? WHERE id = ?",
            (ProjectState.CLOSED.value, now, conclusion, project_id),
        )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            f"Project auto-completed based on OpenClaw evaluation.\n\n{evaluation['reasoning']}",
        )

        # Notify
        await NotificationService(self.db).create_project_result_notification(
            project_id,
            conclusion=conclusion,
        )

    else:
        # Generate follow-up tasks for unmet criteria
        unmet = evaluation.get("unmet_criteria", [])
        if unmet:
            tasks = await reasoning.generate_follow_up_tasks(project_id, unmet)

            for task_data in tasks:
                await self._create_follow_up_task(project_id, task_data, evaluation)

            await self._add_journal_entry(
                project_id,
                JournalEntryType.DECISION,
                f"Generated {len(tasks)} follow-up tasks for unmet criteria: {', '.join(unmet)}",
                {"unmet_criteria": unmet, "evaluation": evaluation},
            )

    return await self._build_project_response(await self._get_project_row(project_id))

async def _generate_conclusion_from_evaluation(
    self,
    project_id: str,
    evaluation: dict[str, Any],
) -> str:
    """Generate project conclusion from evaluation."""

    project = await self._get_project_row(project_id)

    lines = [
        f"## Project Completed: {project['title']}",
        "",
        f"**Aim:** {project.get('aim', 'N/A')}",
        "",
        "**Evaluation:**",
        evaluation.get("reasoning", ""),
        "",
        "**Success Criteria Met:**",
    ]

    for criterion in evaluation.get("met_criteria", []):
        lines.append(f"  ✅ {criterion}")

    lines.extend([
        "",
        "**Conclusion:**",
        f"All success criteria have been satisfied. {project.get('aim', 'The project')} has been successfully completed.",
    ])

    return "\n".join(lines)

async def _create_follow_up_task(
    self,
    project_id: str,
    task_data: dict[str, Any],
    evaluation: dict[str, Any],
) -> None:
    """Create a follow-up task."""

    from uuid import uuid4

    task_id = str(uuid4())
    now = utcnow().isoformat()

    await self.db.execute(
        """
        INSERT INTO tasks (id, title, description, plan, status, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            task_data.get("title", "Follow-up task"),
            task_data.get("description", ""),
            task_data.get("plan", ""),
            "planning",
            task_data.get("priority", "high"),
            now,
            now,
        )
    )

    # Link to project
    await self.db.execute(
        "INSERT INTO project_tasks (project_id, task_id) VALUES (?, ?)",
        (project_id, task_id),
    )

    # Record metadata
    import json
    await self.db.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps({
            "auto_created_by_project": True,
            "autonomy_reason": "unmet_success_criteria",
            "autonomy_evaluation": evaluation,
        }), task_id)
    )
```

**Done when:** A project with all tasks complete triggers evaluation and either completes or creates follow-up tasks.

---

### Step 4: Wire Into Task Completion (30 minutes)

**File:** `cyborg/services/task_service.py`

**Find where tasks are completed and ensure it triggers autonomy:**

```python
async def complete_task(
    self,
    task_id: str,
    result_summary: str | None = None,
    result: str | None = None,
) -> TaskResponse:
    """Complete a task and trigger autonomy check."""

    # ... existing completion logic ...

    # After task is marked complete, trigger autonomy
    from cyborg.services.project_autonomy_service import ProjectAutonomyService

    autonomy = ProjectAutonomyService(self.db)
    await autonomy.on_task_completed(
        task_id=task_id,
        task_title=task_data["title"],
        result_summary=result_summary,
    )

    return task_response
```

**Done when:** Completing a task triggers project evaluation.

---

### Step 5: Manual Testing (2 hours)

**Test Case 1: Successful Auto-Completion**

```bash
# 1. Create a project with simple success criteria
uv run cyborg project create \
  --title "Test Auto-Complete" \
  --aim "Test autonomous completion" \
  --success-criteria '[{"check":"completed_tasks >= 1","description":"Complete at least one task"}]' \
  --auto-execute

# Save the PROJECT_ID

# 2. Create and complete a task
uv run cyborg task create \
  --title "Test task" \
  --plan "Do the thing" \
  --project-id $PROJECT_ID

uv run cyborg task complete $TASK_ID --result-summary "Done"

# 3. Check project status
uv run cyborg project show $PROJECT_ID

# Expected: Project state is "closed"
```

**Test Case 2: Follow-up Task Generation**

```bash
# 1. Create project with criteria you won't meet
uv run cyborg project create \
  --title "Test Follow-up" \
  --aim "Test follow-up generation" \
  --success-criteria '[{"check":"completed_tasks >= 5","description":"Need 5 tasks, will only do 1"}]' \
  --auto-execute

# 2. Create and complete ONE task
uv run cyborg task create \
  --title "Only task" \
  --plan "Just one" \
  --project-id $PROJECT_ID

uv run cyborg task complete $TASK_ID --result-summary "Done"

# 3. Check for new tasks
uv run cyborg task list --project-id $PROJECT_ID

# Expected: New follow-up task created
```

**Test Case 3: Check OpenClaw Was Called**

```bash
# Check OpenClaw logs or Cyborg logs for gateway calls
# Should see calls to session "cyborg:reasoning"
```

---

## Step 6: Fix Issues (2 hours)

**Common problems and fixes:**

**Problem:** "OpenClaw gateway call failed"
```bash
# Check gateway is accessible
openclaw gateway status

# Check Cyborg config
cat .env | grep OPENCLAW

# Test manually
openclaw gateway call agent --params '{"message":"test","deliver":false,"sessionKey":"test"}'
```

**Problem:** "JSON parse error"
```python
# Add logging to see what OpenClaw returned
# In _call_openclaw, add:
print(f"OpenClaw response: {response}")
```

**Problem:** "Project doesn't auto-complete"
```python
# Check if auto_execute is set
# Check if project has open tasks
# Check logs for evaluation result
```

---

## Done Criteria

You're done when:

- [ ] Completing a task triggers OpenClaw evaluation
- [ ] Projects with met criteria auto-close
- [ ] Projects with unmet criteria create follow-up tasks
- [ ] Journal entries record autonomy decisions
- [ ] You've seen at least one full cycle work end-to-end

---

## What You Have (MVP)

**Working:**
- ✅ OpenClaw evaluates success criteria (semantic, not regex)
- ✅ Projects auto-complete when criteria met
- ✅ Follow-up tasks generated for unmet criteria
- ✅ Journal tracks autonomy decisions

**Not Working Yet (Future):**
- ❌ Strategy refinement on failures
- ❌ Learning from past projects
- ❌ Health monitoring
- ❌ Sophisticated context filtering
- ❌ Proper error handling

---

## Tomorrow (If Time Permits)

**Quick wins to add:**
1. Strategy refinement on task failures (+1 hour)
2. Better prompts with examples (+30 min)
3. Per-project autonomy toggle (+30 min)
4. Basic metrics logging (+30 min)

---

## Risks Accepted

- **No tests:** Manual testing only
- **Basic error handling:** Things will break unexpectedly
- **Simple prompts:** Won't handle edge cases
- **No fallback:** If OpenClaw is down, everything stalls
- **Tight coupling:** Only works with OpenClaw
- **Hard-coded values:** timeouts, session keys, etc.

**This is fine for:** Experimental, internal use, learning what works

**Not OK for:** Production, customer-facing, critical projects

---

## Emergency Rollback

If something breaks:

```bash
# Stop Cyborg
systemctl --user stop cyborg

# Revert to previous commit
cd ~/.openclaw/workspace/projects/cyborg
git checkout HEAD~1

# Restart
systemctl --user start cyborg
```

---

Go. Time's ticking.
