# Improved Autonomy for Cyborg

## Executive Summary

This document outlines the plan to extend Cyborg with autonomous project execution capabilities by leveraging OpenClaw for LLM reasoning.

**Key Design Decision:** OpenClaw serves as the "brain" (reasoning/decision-making) while Cyborg serves as the "spine" (state management/orchestration).

### Goals

1. Enable autonomous project execution that iterates toward success criteria
2. Strategic decision-making powered by OpenClaw's LLM reasoning
3. Learning from past projects to improve future performance
4. Maintain clean separation: Cyborg manages state, OpenClaw thinks

### Non-Goals

- Multi-agent competing consumers (deferred)
- Complex session isolation (deferred)
- Alternative LLM provider integrations (OpenClaw only)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CYBORG                                  │
│                   (State & Orchestration)                      │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │   SQLite     │  │   HTTP API   │  │   Journal    │         │
│  │   Database   │  │   Endpoints  │  │   System     │         │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘         │
│         │                 │                  │                  │
│         └─────────────────┴──────────────────┘                  │
│                           │                                      │
│                    ┌──────┴──────┐                              │
│                    │   Detect    │                              │
│                    │   Build     │  (Context Builder)           │
│                    │   Context   │                              │
│                    └──────┬──────┘                              │
│                           │                                      │
│                    ┌──────┴──────┐                              │
│                    │  Ask        │                              │
│                    │  OpenClaw   │◄─────┐                       │
│                    └──────┬──────┘      │                       │
│                           │             │                       │
│                    ┌──────┴──────┐      │                       │
│                    │  Parse      │      │                       │
│                    │  Response   │──────┘                       │
│                    └──────┬──────┘                              │
│                           │                                      │
│                    ┌──────┴──────┐                              │
│                    │  Execute    │                              │
│                    │  Actions    │                              │
│                    │  Record     │                              │
│                    └─────────────┘                              │
└─────────────────────────────────────────────────────────────────┘
                           │
                           │ Gateway RPC
                           │ (agent method)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                        OPENCLAW                                 │
│                      (LLM Brain)                                │
│                                                                  │
│  • Generate project plans                                        │
│  • Evaluate success criteria                                    │
│  • Refine strategy based on progress                             │
│  • Extract learnings from completed projects                     │
│  • Make autonomous decisions                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Components

### 1. Context Builder Service

**File:** `cyborg/services/context_builder.py`

**Purpose:** Build intelligent context for LLM reasoning by gathering relevant project data.

**Responsibilities:**
- Assemble project context (aim, method, plan, criteria)
- Gather task state and results
- Collect journal narrative
- Filter by scope (minimal, standard, comprehensive, full)
- Optimize for token efficiency

**Key Methods:**
```python
async def build_project_context(
    project_id: str,
    scope: ContextScope,  # MINIMAL, STANDARD, COMPREHENSIVE, FULL
    focus_reasoning: str | None,  # "planning", "evaluation", "refinement", "learning"
) -> dict[str, Any]
```

**Context Sizes:**
| Scope | Est. Tokens | Use Case |
|-------|-------------|----------|
| MINIMAL | ~2,000 | Quick checks, status updates |
| STANDARD | ~8,000 | Most operations (default) |
| COMPREHENSIVE | ~25,000 | Strategic decisions |
| FULL | ~50,000+ | Deep analysis (rare) |

---

### 2. OpenClaw Reasoning Service

**File:** `cyborg/services/openclaw_reasoning_service.py`

**Purpose:** Unified interface for all LLM reasoning through OpenClaw gateway.

**Responsibilities:**
- Call OpenClaw gateway with reasoning prompts
- Handle response parsing (JSON/text)
- Error handling and retries
- Use dedicated reasoning session

**Key Methods:**
```python
async def generate_project_plan(
    aim: str,
    method: str | None = None,
    success_criteria: list[str] | None = None,
    reference_project_id: str | None = None,
) -> list[dict[str, Any]]

async def evaluate_success_criteria(
    project_id: str,
) -> dict[str, Any]

async def refine_project_strategy(
    project_id: str,
    trigger_task_id: str,
) -> dict[str, Any]

async def extract_learnings(
    project_id: str,
) -> list[dict[str, Any]]

async def generate_task_plan(
    task_id: str,
) -> str

async def analyze_project_health(
    project_id: str,
) -> dict[str, Any]
```

**Gateway Integration:**
- Uses existing `OpenClawHookService`
- Calls `agent` method with reasoning prompt
- Dedicated session: `cyborg:reasoning`
- Response format hints (JSON vs text)
- Timeout per request type

---

### 3. Enhanced Autonomy Service

**File:** `cyborg/services/project_autonomy_service.py` (modify existing)

**Changes:**
- Integrate `OpenClawReasoningService`
- Use semantic evaluation instead of regex
- Generate follow-up tasks using LLM
- Add strategy refinement trigger

**New Methods:**
```python
async def refine_strategy_if_needed(
    project_id: str,
    trigger_task_id: str,
) -> None
    """Ask OpenClaw to analyze and suggest strategy refinement."""

async def checkpoint_with_refinement(
    project_id: str,
    completed_task_id: str,
) -> None
    """After task completion, evaluate and potentially refine strategy."""
```

---

### 4. Learning Service (New)

**File:** `cyborg/services/learning_service.py`

**Purpose:** Extract and apply learnings from completed projects.

**Responsibilities:**
- Extract insights after project completion
- Store in `project_insights` table
- Suggest success criteria for new projects based on patterns
- Provide context for planning based on similar past projects

**Key Methods:**
```python
async def extract_and_store_insights(
    project_id: str,
) -> list[dict[str, Any]]

async def suggest_success_criteria(
    project_aim: str,
    project_method: str,
) -> list[SuccessCriterion]

async def get_similar_projects(
    aim: str,
    limit: int = 3,
) -> list[dict[str, Any]]
```

---

### 5. Health Monitor Service (New)

**File:** `cyborg/services/health_monitor_service.py`

**Purpose:** Proactively identify at-risk projects.

**Responsibilities:**
- Scheduled health checks (cron-like)
- Analyze blocked tasks, delays, failures
- Ask OpenClaw for risk assessment
- Generate alerts and recommendations

**Key Methods:**
```python
async def scan_all_projects(
) -> list[dict[str, Any]]

async def analyze_single_project(
    project_id: str,
) -> dict[str, Any]

async def schedule_health_checks(
) -> None
```

---

## Database Schema Changes

### New Tables

```sql
-- cyborg/schemas/130_work_queue.sql (if pull model added later)

-- cyborg/schemas/140_project_insights.sql
CREATE TABLE project_insights (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    outcome_type TEXT NOT NULL,  -- 'success', 'failure', 'partial'
    insight_category TEXT NOT NULL,  -- 'planning', 'execution', 'estimation', 'technical'
    insight_data JSON NOT NULL,  -- The learned insight
    applicability_pattern JSON,  -- When this insight applies
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_project_insights_project (project_id),
    INDEX idx_project_insights_category (insight_category),
    INDEX idx_project_insights_outcome (outcome_type)
);

-- cyborg/schemas/150_health_checks.sql
CREATE TABLE project_health_checks (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    check_type TEXT NOT NULL,  -- 'schedule', 'anomaly', 'milestone'
    health_score REAL CHECK (health_score >= 0 AND health_score <= 1),
    indicators JSON,
    risk_level TEXT CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    alert_triggered BOOLEAN DEFAULT FALSE,
    recommendations JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_health_checks_project (project_id),
    INDEX idx_health_checks_risk (risk_level),
    INDEX idx_health_checks_created (created_at)
);
```

---

## API Changes

### New Endpoints

```python
# cyborg/routers/planning.py

@router.post("/api/v1/planning/generate-plan")
async def generate_project_plan(
    request: PlanGenerationRequest,
    db: Database = Depends(get_database),
) -> PlanGenerationResponse:
    """Generate a project plan using OpenClaw reasoning."""

@router.post("/api/v1/projects/{project_id}/refine-strategy")
async def refine_project_strategy(
    project_id: str,
    request: StrategyRefinementRequest,
    db: Database = Depends(get_database),
) -> StrategyRefinementResponse:
    """Trigger strategy refinement analysis."""

# cyborg/routers/health.py

@router.get("/api/v1/health/scan")
async def scan_project_health(
    db: Database = Depends(get_database),
) -> HealthScanResponse:
    """Scan all projects for health issues."""

@router.get("/api/v1/projects/{project_id}/health")
async def get_project_health(
    project_id: str,
    db: Database = Depends(get_database),
) -> ProjectHealthResponse:
    """Get health analysis for a specific project."""

# cyborg/routers/learning.py

@router.post("/api/v1/projects/{project_id}/extract-insights")
async def extract_project_insights(
    project_id: str,
    db: Database = Depends(get_database),
) -> InsightsResponse:
    """Extract and store insights from a completed project."""

@router.get("/api/v1/learning/similar-projects")
async def find_similar_projects(
    aim: str,
    limit: int = 3,
    db: Database = Depends(get_database),
) -> SimilarProjectsResponse:
    """Find similar past projects for reference."""
```

---

## OpenClaw Integration

### Gateway Usage

All reasoning uses the existing `OpenClawHookService` gateway client:

```python
# Reasoning request pattern
params = {
    "message": prompt,  # Context + question
    "deliver": False,  # Not delivering to user
    "sessionKey": "cyborg:reasoning",  # Dedicated reasoning session
    "thinking": "verbose",
    "timeout": timeout_ms,
    "idempotencyKey": str(uuid4()),
}

response = await openclaw_service._send_gateway_request(
    method="agent",
    params=params,
    expect_final=True,
    timeout_seconds=timeout,
)
```

### Reasoning Session

**Session Key:** `cyborg:reasoning`

**Purpose:** Dedicated session for autonomous reasoning, separate from user-facing sessions.

**Benefits:**
- Clean separation of user chat vs autonomous thinking
- Can have different model/temperature settings
- Easier to monitor/debug reasoning decisions
- Doesn't pollute user session history

### Prompt Engineering

Each reasoning type has a purpose-built prompt:

1. **Plan Generation:** Structured output, creative but grounded
2. **Criteria Evaluation:** Analytical, precise, uses evidence
3. **Strategy Refinement:** Critical thinking, considers trade-offs
4. **Learning:** Pattern recognition, abstracts principles
5. **Health Analysis:** Risk assessment, forward-looking

All prompts:
- Include relevant context
- Specify exact response format (JSON schema)
- Include examples in prompt for complex outputs
- Set appropriate temperature (0.3-0.5)

---

## Autonomy Loop Flow

```
1. TRIGGER
   ├─ Task completes
   ├─ Deadline approaches
   ├─ Health check runs
   └─ User requests action

2. DETECT (Cyborg)
   ├─ Identify what changed
   ├─ Determine affected projects
   └─ Check if action needed

3. BUILD CONTEXT (Cyborg)
   ├─ Gather project data
   ├─ Collect task results
   ├─ Include journal entries
   └─ Filter by scope/focus

4. ASK OPENCLAW (Reasoning)
   ├─ Send context + prompt
   ├─ Wait for decision
   └─ Parse structured response

5. EXECUTE (Cyborg)
   ├─ Create/update tasks
   ├─ Update project state
   ├─ Send notifications
   ├─ Record journal entry
   └─ Trigger next actions

6. RECORD (Cyborg)
   ├─ Store decision in journal
   ├─ Update audit history
   └─ Loop back to step 1
```

---

## Implementation Progress

### ✅ Phase 1: Foundation Services (COMPLETED)

**Status:** All tasks complete

**Completed:**
- [x] Implement `ContextBuilder` service with scope support (MINIMAL, STANDARD, COMPREHENSIVE, FULL)
- [x] Implement `OpenClawReasoningService` with all reasoning methods
- [x] Add database schemas (140_project_insights.sql, 150_health_checks.sql)
- [x] Write unit tests for context assembly
- [x] Write integration tests for gateway reasoning calls

**Files Created:**
- `cyborg/services/context_builder.py` - Full context builder with scope filtering
- `cyborg/services/openclaw_reasoning_service.py` - Complete reasoning service
- `cyborg/schemas/140_project_insights.sql` - Insights table (with IF NOT EXISTS)
- `cyborg/schemas/150_health_checks.sql` - Health checks table (with IF NOT EXISTS)
- `tests/test_context_builder.py` - Context builder tests
- `tests/test_openclaw_reasoning.py` - Reasoning service tests

**Bug Fixes:**
- Fixed migration idempotency by adding IF NOT EXISTS to all schema objects
- Fixed SQL syntax error in context_builder.py (removed non-existent pt.order column)
- Fixed SQLite string/int conversion in rule-based evaluation

---

### ✅ Phase 2: Core Autonomy Integration (COMPLETED)

**Status:** Core features implemented, minor issues to fix

**Completed:**
- [x] Update `ProjectAutonomyService` to use OpenClaw reasoning
- [x] Implement LLM-based follow-up task generation
- [x] Add strategy refinement trigger with auto-accept
- [x] Write integration tests for autonomy loop
- [x] Add `cyborg_service_url` to webhook payloads for dynamic service discovery
- [ ] Fix missing `generate_follow_up_tasks` method in OpenClawReasoningService
- [ ] End-to-end testing (blocked by above issues)

**Files Modified:**
- `cyborg/services/project_autonomy_service.py` - Added checkpoint_and_refine, updated on_task_completed
- `cyborg/services/project_execution_service.py` - Updated evaluate_and_complete to use LLM
- `cyborg/services/webhook_service.py` - Added cyborg_service_url support
- `cyborg/services/openclaw_hook_service.py` - Include service URL in messages
- `cyborg/config.py` - Added public_url setting and CYBORG_PUBLIC_URL env var
- `cyborg/routers/context.py` - Removed circular discovery endpoint
- `openclaw-plugin/SKILL.md` - Updated to document webhook-based URL discovery

**New Capabilities:**
- OpenClaw evaluates success criteria (semantic, not regex)
- OpenClaw generates contextual follow-up tasks
- Projects auto-refine strategy on task completion
- All decisions recorded in journal
- Per-project opt-out available (metadata.auto_refine)
- Dynamic service URL via webhook metadata

**Known Issues:**
1. `OpenClawReasoningService.generate_follow_up_tasks` method doesn't exist
2. Fallback error handling uses `self.db.logger` which doesn't exist (fixed partially)

---

### 🔄 Phase 2.5: E2E Testing Infrastructure (NEW - IN PROGRESS)

**Status:** Just added to plan

**Goals:** Create deterministic end-to-end tests without OpenClaw dependency

**Tasks:**
- [ ] Create `MockLLMReasoningService` for testing
- [ ] Implement `tests/mocks/mock_llm_service.py`
- [ ] Write `tests/test_autonomy_e2e.py` with test scenarios
- [ ] Add `tests/test_autonomy_scenarios.py` for comprehensive coverage
- [ ] Fix remaining bugs discovered during testing:
  - [ ] Add `generate_follow_up_tasks` to OpenClawReasoningService
  - [ ] Verify fallback behavior works correctly
- [ ] Run full test suite and ensure all pass

**Deliverables:**
- Mock LLM service that emulates OpenClaw responses
- End-to-end test scenarios covering:
  - Simple project completion
  - Follow-up task generation
  - Strategy refinement
  - Multiple concurrent projects
  - Error conditions

**Success Criteria:**
- All autonomy scenarios pass deterministically
- Tests run without OpenClaw being available
- Coverage > 80% for autonomy code paths

---

### Phase 3: Planning & Strategy (Week 5-6)

**Goals:** Add strategic reasoning capabilities

**Tasks:**
- [ ] Implement `generate_project_plan` reasoning
- [ ] Implement `refine_project_strategy` reasoning
- [ ] Add strategy refinement trigger points
- [ ] CLI commands for plan generation
- [ ] API endpoints for external tools

**Deliverables:**
- AI-generated project plans
- Strategy refinement on failures
- CLI/API integration

**Success Criteria:**
- Can generate coherent project plans
- Strategy refinements are actionable
- Users can trigger via CLI/API

---

### Phase 4: Learning & Health (Week 7-8)

**Goals:** Add proactive monitoring and learning

**Tasks:**
- [ ] Implement `LearningService`
- [ ] Implement `HealthMonitorService`
- [ ] Add scheduled health check runner
- [ ] Implement insight extraction on project close
- [ ] Dashboard/visibility for health status

**Deliverables:**
- Automated health monitoring
- Learning extraction from completed projects
- Health status API

**Success Criteria:**
- Health checks run on schedule
- Insights are extracted and stored
- Can query similar projects

---

### Phase 5: Polish & Integration (Week 9-10)

**Goals:** Production hardening

**Tasks:**
- [ ] Error handling and retries
- [ ] Rate limiting for OpenClaw calls
- [ ] Monitoring and observability
- [ ] Documentation
- [ ] Performance optimization
- [ ] End-to-end testing

**Deliverables:**
- Production-ready system
- Complete documentation
- Monitoring dashboards

**Success Criteria:**
- No unhandled errors in normal flow
- System handles OpenClaw unavailability gracefully
- Documentation is complete

---

## Configuration

### New Settings

```python
# cyborg/config.py

class AutonomySettings(BaseSettings):
    """Settings for autonomous features."""

    # OpenClaw reasoning
    reasoning_session_key: str = "cyborg:reasoning"
    reasoning_timeout_default: int = 30  # seconds
    reasoning_timeout_planning: int = 60
    reasoning_timeout_evaluation: int = 45
    reasoning_timeout_refinement: int = 60

    # Health monitoring
    health_check_interval_minutes: int = 60
    health_check_enabled: bool = True

    # Learning
    auto_extract_insights: bool = True
    insight_min_project_age_days: int = 7  # Don't extract from very short projects

    # Context defaults
    default_context_scope: str = "standard"
    max_context_tokens: int = 30000

    class Config:
        env_prefix = "CYBORG_AUTONOMY_"
```

---

## Testing Strategy

### End-to-End Automated Tests (New)

**Problem:** OpenClaw may not be available during testing, and we need deterministic tests.

**Solution:** Create a mock LLM service that emulates OpenClaw reasoning responses.

```python
# tests/mocks/mock_llm_service.py

class MockLLMReasoningService:
    """Mock LLM service for deterministic testing without OpenClaw."""

    def __init__(self, db: Database):
        self.db = db
        self.responses = self._load_test_responses()

    async def evaluate_success_criteria(self, project_id: str) -> dict[str, Any]:
        """Return pre-defined evaluation based on project state."""
        project = await self._get_project(project_id)

        # Rule-based deterministic evaluation for testing
        context, met_criteria, unmet_criteria = await self._evaluate_rule_based(project_id)

        return {
            "all_met": len(unmet_criteria) == 0,
            "met_criteria": [c.description for c in met_criteria],
            "unmet_criteria": [c.description for c in unmet_criteria],
            "reasoning": "Mock evaluation based on project state",
        }

    async def refine_project_strategy(
        self,
        project_id: str,
        trigger_task_id: str
    ) -> dict[str, Any]:
        """Return mock refinement response."""
        return {
            "should_refine": False,
            "reasoning": "Project progressing well (mock)",
            "suggested_changes": [],
            "new_priorities": {},
            "risks_identified": [],
        }

    async def extract_learnings(self, project_id: str) -> list[dict[str, Any]]:
        """Return mock learnings."""
        return [
            {
                "category": "execution",
                "insight": "Mock insight: Small tasks complete faster",
                "applicability": {"keywords": ["test", "small"]},
            }
        ]
```

**Usage in Tests:**

```python
# tests/test_autonomy_e2e.py

async def test_full_autonomy_loop_with_mock_llm():
    """Test complete autonomy flow from task creation to project completion."""

    # Setup: Use test database and mock LLM
    db = Database(db_path=Path("/tmp/cyborg-e2e-test.db"))
    await db.connect()
    await db.apply_migrations()

    # Replace OpenClaw service with mock
    from cyborg.services.project_execution_service import ProjectExecutionService
    from tests.mocks.mock_llm_service import MockLLMReasoningService

    execution_service = ProjectExecutionService(db)
    execution_service.reasoning_service = MockLLMReasoningService(db)

    # Create project with success criteria
    project = await create_test_project(
        success_criteria=[{"check": "completed_tasks >= 2", "description": "Complete 2 tasks"}],
        auto_execute=True,
    )

    # Create and complete tasks
    task1 = await create_task(project_id=project.id)
    await complete_task(task1.id)

    task2 = await create_task(project_id=project.id)
    await complete_task(task2.id)

    # Assert: Project auto-completed
    updated_project = await get_project(project.id)
    assert updated_project.state == "closed"
    assert updated_project.conclusion is not None
```

### Test Scenarios

**File:** `tests/test_autonomy_scenarios.py`

```python
# Scenario 1: Simple completion
async def test_project_auto_completes_when_criteria_met():
    """Given: Project with criteria "completed_tasks >= 2"
       When: 2 tasks are completed
       Then: Project closes with conclusion"""

# Scenario 2: Follow-up task generation
async def test_generates_follow_up_tasks_when_criteria_unmet():
    """Given: Project with criteria "completed_tasks >= 5"
       When: Only 2 tasks completed
       Then: Follow-up tasks created"""

# Scenario 3: Strategy refinement
async def test_refines_strategy_when_tasks_fail():
    """Given: Auto-refine enabled project
       When: Task fails
       Then: Strategy refinement triggered, changes applied"""

# Scenario 4: No auto-refinement when disabled
async def test_skips_refinement_when_disabled():
    """Given: Project with metadata.auto_refine = false
       When: Task completes
       Then: No refinement triggered"""

# Scenario 5: Multiple projects
async def test_autonomy_works_across_concurrent_projects():
    """Given: 3 projects with different criteria
       When: Tasks complete across projects
       Then: Each project evaluates independently"""
```

### Unit Tests

Each service has comprehensive unit tests:

```python
# tests/test_context_builder.py
async def test_build_minimal_context():
    """Test minimal context assembly."""
    context = await builder.build_project_context(
        project_id="test-123",
        scope=ContextScope.MINIMAL,
    )
    assert context["core"]["project"]["id"] == "test-123"
    assert estimate_tokens(context) < 3000

async def test_context_filters_by_focus():
    """Test context includes relevant items based on focus."""
    context = await builder.build_project_context(
        project_id="test-123",
        scope=ContextScope.STANDARD,
        focus_reasoning="evaluation",
    )
    # Should include failed tasks for evaluation
    assert any(t["status"] == "failed" for t in context["tasks"]["tasks"])
```

### Integration Tests

```python
# tests/test_openclaw_reasoning.py
async def test_evaluate_success_criteria_e2e():
    """Test full flow: context build → OpenClaw call → parse response."""
    # Setup: Create project with tasks
    project_id = await create_test_project()

    # Execute
    result = await reasoning_service.evaluate_success_criteria(project_id)

    # Assert
    assert "all_met" in result
    assert "reasoning" in result
    assert isinstance(result["met_criteria"], list)
```

### Contract Tests

Test that OpenClaw responses match expected format:

```python
async def test_plan_generation_response_format():
    """Verify OpenClaw returns expected JSON structure."""
    response = await reasoning_service.generate_project_plan(
        aim="Test aim",
        method="Test method",
        success_criteria=["Criterion 1"],
    )

    assert isinstance(response, list)
    assert all("title" in step for step in response)
    assert all("description" in step for step in response)
    assert all("criteria" in step for step in response)
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| **OpenClaw unavailable** | High | Graceful degradation, queue reasoning requests, retry with backoff |
| **Malformed OpenClaw responses** | Medium | Schema validation, fallback to rule-based logic, error logging |
| **Context too large** | Medium | Scope filtering, summarization, token counting with warnings |
| **Reasoning quality varies** | Medium | Consistent prompt engineering, temperature tuning, example in prompt |
| **Slow reasoning responses** | Low | Async operations, timeouts, progress indicators |
| **Cost of frequent LLM calls** | Low | Cache where appropriate, use appropriate scope, batch operations |

---

## Rollout Strategy

### Stage 1: Development (Weeks 1-10)

- Feature branch: `feature/autonomy`
- All development and testing happens here
- No impact to production

### Stage 2: Beta (Weeks 11-12)

- Deploy to staging environment
- Enable for test projects only
- Monitor reasoning quality and performance
- Gather feedback from internal users

### Stage 3: Gradual Rollout (Weeks 13-14)

- Feature flag: `autonomy_enabled` (default: false)
- Enable for specific projects: `project.metadata.autonomy_enabled = true`
- Monitor metrics:
  - Reasoning call success rate
  - Response times
  - Decision quality (manual review)

### Stage 4: Full Launch (Week 15+)

- Feature flag default: true
- Opt-out available per project
- Production monitoring active

---

## Success Metrics

### Technical Metrics

- **Reasoning success rate:** > 95%
- **Average response time:** < 10 seconds
- **Context assembly time:** < 2 seconds
- **System uptime:** > 99.5%

### Outcome Metrics

- **Projects auto-completed:** Track over time
- **Follow-up task quality:** Manual review sample
- **Strategy refinements adopted:** % accepted
- **Insights extracted:** Count and utility

### User Satisfaction

- **Time to project completion:** Compare before/after
- **Manual intervention rate:** Should decrease for autonomous projects
- **User feedback:** Qualitative surveys

---

## Design Decisions

1. **Learning application: Auto-apply**
   - Learnings from completed projects are automatically applied to new similar projects
   - No human approval required for learned insights
   - Learning quality is monitored via outcomes
   - Poor learnings are flagged and can be disabled

2. **Plan refinement: Auto-accept**
   - OpenClaw strategy refinements are automatically applied
   - Projects can change direction without human approval
   - All refinements are recorded in journal with full reasoning
   - Users can disable auto-refinement per-project if needed

3. **Reasoning provider: OpenClaw only**
   - All LLM reasoning goes through OpenClaw gateway
   - No direct Anthropic/OpenAI integration
   - Keeps architecture simple and consistent

4. **OpenClaw unavailable: Stall**
   - Projects pause when OpenClaw cannot be reached
   - Notifications sent to users about stall
   - Automatic retry with exponential backoff
   - No fallback to rule-based logic (maintains decision quality)

---

## Implications of Aggressive Autonomy

These decisions create a **highly autonomous system** with important implications:

### Benefits

- **True hands-off operation:** Projects can iterate toward success criteria without human intervention
- **Continuous improvement:** Each project makes future projects smarter
- **Adaptive execution:** Projects can pivot when approaches aren't working
- **Minimal friction:** No approval gates slowing down progress

### Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| **Bad learnings propagate** | Track insight effectiveness, disable low-quality insights, monitor project outcomes |
| **Unwanted plan changes** | Full journal audit trail, per-project opt-out, notification on major changes |
| **Runaway autonomy** | Circuit breakers (max autonomy cycles), human escalation triggers, confidence thresholds |
| **Stalled projects** | Aggressive retry, user notifications, manual override capability |

### Operational Considerations

**Project metadata controls:**
```python
metadata = {
    "auto_refine": true,           # Allow automatic plan changes (default: true)
    "auto_apply_learnings": true,   # Use insights from similar projects (default: true)
    "max_autonomy_cycles": 10,      # Prevent infinite loops (default: 10)
    "human_escalation": "on_failure",  # When to notify human
}
```

**Monitoring requirements:**
- Track insight effectiveness scores
- Monitor autonomy cycle counts per project
- Alert on patterns of failures
- Dashboard showing active autonomous decisions

**Rollout strategy:**
- Start with conservative confidence thresholds
- Gradually increase autonomy as trust builds
- Keep per-project opt-out available
- Monitor first 100 autonomous decisions closely

---

## Future Enhancements (Out of Scope)

- Pull-based work queue (OpenClaw fetches tasks)
- Multi-agent collaboration
- Advanced RAG for context retrieval
- Cost optimization (model selection per task)
- Streaming responses for long-running reasoning
- Human-in-the-loop approval gates
- Cross-project learning patterns

---

## References

- Current project autonomy design: `project-autonomy.md`
- OpenClaw integration: `OPENCLAW-INTEGRATION.md`
- Existing autonomy services: `cyborg/services/project_autonomy_service.py`
- Database schemas: `cyborg/schemas/`

---

## Document Version

- **Created:** 2025-01-21
- **Author:** Generated via Cyborg planning
- **Status:** Draft for review
- **Next Review:** After Phase 1 completion
