# Prompt Examples

Reference examples for each of the 10 prompt categories logged in `prompt_history`.

## 1. `plan_generation`

Used when generating a new project plan from an objective.

**Input data:**
```python
aim = "Build a customer portal"
method = "Use FastAPI and PostgreSQL"
success_criteria = ["Portal handles 1000 users", "Users can view orders"]
```

**Rendered prompt:**
```
You are a project planning assistant. Generate a structured execution plan.

Project Aim: Build a customer portal
Method: Use FastAPI and PostgreSQL

Success Criteria:
  1. Portal handles 1000 users
  2. Users can view orders

Generate a plan with 3-8 steps. Each step should have:
  - title: Brief step name
  - description: What needs to be done
  - criteria: How to know this step is complete

Respond with valid JSON only:
{
  "steps": [
    {"order": 0, "title": "...", "description": "...", "criteria": "..."},
    ...
  ]
}
```

## 2. `criteria_evaluation`

Used when evaluating whether a project's success criteria have been met.

**Input data:**
```python
project_id = "abc-123"
# Built from project context including tasks and journal entries
```

**Rendered prompt:**
```
You are evaluating whether a project has achieved its success criteria.

Project: Test Evaluation Project
Aim: Test automated evaluation

Success Criteria to Evaluate:
  1. Complete at least 2 tasks
     Check: completed_tasks >= 2
  2. No failed tasks
     Check: failed_tasks == 0

Current State:
  - Total tasks: 2
  - Completed: 2
  - Failed: 0
  - Active: 0

Recent Journal:
  - [info] Project created...

Evaluate each criterion based on available evidence.
Respond with valid JSON only:
{
  "all_met": true or false,
  "met_criteria": ["criterion 1", "criterion 2"],
  "unmet_criteria": ["criterion 3"],
  "reasoning": "Brief explanation of the evaluation..."
}
```

## 3. `strategy_refinement`

Used after a task completes to analyze project progress and suggest refinements.

**Input data:**
```python
project_id = "abc-123"
trigger_task_id = "task-456"
```

**Rendered prompt:**
```
Analyze this project's progress and suggest strategic refinements.

Project: Needs Refinement
Aim: Test refinement with changes
Current State: active

Trigger: Task task-456 just completed.
Status: completed, Result: Task finished successfully

Task Summary:
  - Total: 4
  - Completed: 2
  - Failed: 1
  - Active: 1

Recent Activity:
  - [decision] Switched to incremental approach...

Consider:
1. Is the current plan still optimal?
2. Are there blockers or risks?
3. Should tasks be re-prioritized?
4. Are additional steps needed?

Respond with valid JSON only:
{
  "should_refine": true or false,
  "reasoning": "...",
  "suggested_changes": [
    {"type": "add_task|remove_task|reprioritize|change_approach", "description": "..."}
  ],
  "new_priorities": {"task_id": "high|medium|low"},
  "risks_identified": ["..."]
}
```

## 4. `learning_extraction`

Used when extracting insights from a completed project.

**Input data:**
```python
project_id = "abc-123"
# Full project context with journal entries
```

**Rendered prompt:**
```
Extract insights and learnings from this completed project.

Project: Completed for Learning
Aim: Extract lessons
Duration: 14 days
Outcome: closed

Tasks:
  - Total: 6
  - Completed: 5
  - Failed: 1

Full Journal:
  - [info] Project started...
  - [blocker] Database migration failed...

Extract:
1. What worked well?
2. What didn't work?
3. What would you do differently?
4. Patterns that could apply to future projects?

Respond with valid JSON only:
{
  "insights": [
    {
      "category": "planning|execution|estimation|communication|technical",
      "lesson": "What was learned",
      "applicability": "when to apply this",
      "impact": "positive|negative|neutral"
    }
  ],
  "success_patterns": ["..."],
  "failure_patterns": ["..."],
  "recommendations": ["..."]
}
```

## 5. `task_planning`

Used to generate an execution plan for a specific task.

**Input data:**
```python
task_id = "task-789"
# Task with optional project context
```

**Rendered prompt:**
```
Generate an execution plan for this task.

Task: Complex Task
Description: Needs detailed planning

Parent Project: Test Project
Project Aim: Test project for task plan generation

Generate a concise plan (3-5 bullet points) for executing this task.
Be specific and actionable.
```

## 6. `health_analysis`

Used to analyze project health and identify risks.

**Input data:**
```python
project_id = "abc-123"
# Standard project context
```

**Rendered prompt:**
```
Analyze the health of this project and identify risks.

Project: At Risk Project
State: active
Duration: 21 days

Task Summary:
  - Total: 10
  - Completed: 3
  - Active: 2
  - Blocked: 4
  - Failed: 1

Blocked Tasks:
  - API Integration: Waiting for external service

Recent Journal (concerns, decisions, blockers):
  - [blocker] External API rate limiting...

Assess:
1. Is this project at risk?
2. What are the critical blockers?
3. Is the schedule at risk?
4. What immediate actions are needed?

Respond with valid JSON only:
{
  "health_status": "healthy|at_risk|critical",
  "risk_level": "low|medium|high|critical",
  ...
}
```

## 7. `follow_up_generation`

Used to generate follow-up tasks for unmet project criteria.

**Input data:**
```python
project_id = "abc-123"
unmet_criteria = ["Need 5 completed tasks", "All tests must pass"]
```

**Rendered prompt:**
```
Generate follow-up tasks for this project.

Project: Follow-up Test
Aim: Test follow-up generation
Method: N/A

Task Summary:
  - Total: 2
  - Completed: 1
  - Failed: 1
  - Blocked: 0

Unmet Criteria:
  - Need 5 completed tasks
  - All tests must pass

Recent Journal:
  - [info] Task completed...

Suggest 1-3 concrete follow-up tasks that would help satisfy the unmet criteria.
Each task must include:
  - title
  - description
  - plan
  - priority (low|medium|high|critical)

Respond with valid JSON only:
{
  "tasks": [
    {"title": "...", "description": "...", "plan": "...", "priority": "high"}
  ]
}
```

## 8. `task_assignment`

Used when dispatching a task assignment notification to an OpenClaw session.

**Input data:**
```python
notification = {
    "id": "notif-1",
    "title": "Research competitor pricing",
    "message": "Analyze the top 5 competitors and summarize pricing models.",
    "notification_type": "task_assignment",
    "metadata": {
        "task_id": "task-1",
        "parent_project_id": "proj-1",
        "parent_project_title": "Market Research",
        "target_session": {"kind": "whatsapp"},
        "channel": "whatsapp",
        "session_key": "sess-src",
    },
}
session_key = "sess-dst"
```

**Rendered prompt:**
```
Cyborg task assignment for this session.

You are responsible for handling this task in the current session.
Use the user's replies here as task input, ask focused follow-up questions if needed,
and complete or fail the Cyborg task once you have a clear answer.
This turn should send the first natural user-facing message to the recipient.

Task ID: task-1
Notification ID: notif-1
Session Key: sess-dst
Task: Research competitor pricing

Task brief:
Analyze the top 5 competitors and summarize pricing models.

Parent project: Market Research (proj-1)

Source session:
- channel: whatsapp
- session_key: sess-src
- chat_id: unknown

Target session:
- kind: whatsapp
- session_key: sess-dst
- recipient: +1234567890

Instructions:
- Send one concise natural message now that asks the first question needed to progress the task.
- Do not mention Cyborg, hidden setup, task IDs, notification IDs, or internal routing.
- Treat the next user reply in this session as work on this task.
- If the answer is incomplete, ask one focused follow-up at a time.
- Once the task is answered, complete the Cyborg task with the exact answer using: cyborg task complete <task-id> --result-summary "<answer>".
- Keep the tone natural for the channel and recipient.
```

## 9. `needs_input`

Used when dispatching a notification that requires user approval or input.

**Input data:**
```python
notification = {
    "id": "notif-2",
    "title": "Plan approval needed",
    "message": "The project plan has been generated and needs your approval.",
    "notification_type": "needs_input",
    "metadata": {
        "task_id": "task-2",
        "parent_project_id": "proj-2",
        "parent_project_title": "Build API",
    },
}
session_key = "sess-ni"
```

**Rendered prompt:**
```
Cyborg notification: approval or input needed.

The user needs to review and respond to a Cyborg request.
Your task is to:
1. Show thinking about what needs approval
2. Present the request clearly to the user
3. Help them understand what action is needed

Notification ID: notif-2
Type: needs_input

Request: Plan approval needed

The project plan has been generated and needs your approval.

Task ID: task-2

Project: Build API (proj-2)

Instructions:
- Send a natural message to the recipient asking for the needed approval/input.
- Include relevant details from the request above.
- Do not mention Cyborg internal details like notification IDs unless necessary.
- Keep the tone appropriate for the channel (WhatsApp DM).

Once the user approves, respond to this notification by calling: cyborg task plan approve task-2
Or use the HTTP API: PUT /api/v1/tasks/task-2/plan with plan approval details.
```

## 10. `notification`

Used for generic notification delivery (status updates, info messages, etc.).

**Input data:**
```python
notification = {
    "id": "notif-3",
    "title": "Task Completed",
    "message": "The verification task has been completed successfully.",
    "entity_type": "task",
    "metadata": {
        "project_id": "proj-3",
        "parent_project_title": "Portal Build",
    },
}
```

**Rendered prompt:**
```
Task Completed

The verification task has been completed successfully.

Project: Portal Build

Notification ID: notif-3
```
