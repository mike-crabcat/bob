"""Pydantic models for the Cyborg data service."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MetadataDict = dict[str, Any]
COLOR_PATTERN = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


def validate_cron_expression(expression: str) -> str:
    """Validate a simple five-field cron expression."""

    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron expressions must contain exactly five fields")

    ranges = (
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 7),
    )
    for part, (lower, upper) in zip(fields, ranges, strict=True):
        for token in part.split(","):
            _validate_cron_token(token, lower, upper)
    return expression


def _validate_cron_token(token: str, lower: int, upper: int) -> None:
    if token == "*":
        return
    if "/" in token:
        base, step = token.split("/", 1)
        if not step.isdigit() or int(step) <= 0:
            raise ValueError("Cron step values must be positive integers")
        _validate_cron_token(base, lower, upper)
        return
    if token.startswith("*/"):
        step = token[2:]
        if not step.isdigit() or int(step) <= 0:
            raise ValueError("Cron step values must be positive integers")
        return
    if "-" in token:
        start, end = token.split("-", 1)
        if not (start.isdigit() and end.isdigit()):
            raise ValueError("Cron ranges must use integers")
        start_value = int(start)
        end_value = int(end)
        if not (lower <= start_value <= upper and lower <= end_value <= upper):
            raise ValueError("Cron values are out of range")
        if start_value > end_value:
            raise ValueError("Cron range start must be <= range end")
        return
    if token.isdigit():
        value = int(token)
        if not lower <= value <= upper:
            raise ValueError("Cron values are out of range")
        return
    raise ValueError(f"Unsupported cron token: {token}")


class CyborgModel(BaseModel):
    """Shared model configuration."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    DEPRECATED = "deprecated"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskTargetSessionKind(StrEnum):
    GROUP = "group"
    DM = "dm"


class SessionRouteKind(StrEnum):
    GROUP = "group"
    DM = "dm"


class TaskStepStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class ProjectState(StrEnum):
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class ProjectSpecStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlanStep(CyborgModel):
    """A single step in a project execution plan."""
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    criteria: str = Field(min_length=1, description="Criteria to determine if this step is satisfied")
    order: int = Field(ge=0, description="Execution order (0-based)")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("description must not be blank")
        return stripped


class SuccessCriterion(CyborgModel):
    """A criterion for determining if a project's aim has been achieved."""
    check: str = Field(min_length=1, description="Expression or condition to evaluate (e.g., 'endpoint_count > 10')")
    description: str = Field(min_length=1, description="Human-readable description of what this checks")

    @field_validator("check")
    @classmethod
    def check_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("check must not be blank")
        return stripped

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("description must not be blank")
        return stripped


class JournalEntryType(StrEnum):
    NOTE = "note"
    MILESTONE = "milestone"
    DECISION = "decision"
    BLOCKER = "blocker"
    RESULT = "result"


class EventStatus(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class NotificationEntityType(StrEnum):
    TASK = "task"
    PROJECT = "project"
    EVENT = "event"


class NotificationType(StrEnum):
    NEEDS_INPUT = "needs_input"
    EVENT_REMINDER = "event_reminder"
    TASK_ASSIGNMENT = "task_assignment"
    TASK_RESULT = "task_result"
    PROJECT_RESULT = "project_result"
    TASK_RETRY = "task_retry"
    TASK_INPUT_RESPONSE = "task_input_response"
    TASK_TAP = "task_tap"
    SUBMISSION_REVIEW = "submission_review"
    NEXT_ACTION = "next_action"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class NotificationDeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"


class RecipientType(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    CHANNEL = "channel"


class RecipientStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    TENTATIVE = "tentative"


class RetryAction(StrEnum):
    RETRY = "retry"
    RETRY_FROM = "retry_from"
    ESCALATE = "escalate"
    ABORT = "abort"


class RetryConfig(CyborgModel):
    """Retry policy for a task."""

    max_attempts: int = Field(default=1, ge=1)
    current_attempt: int = Field(default=0, ge=0)
    on_failure: RetryAction = RetryAction.ABORT
    retry_from_step: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_strategy(self) -> "RetryConfig":
        if self.current_attempt > self.max_attempts:
            raise ValueError("current_attempt cannot exceed max_attempts")
        if self.on_failure == RetryAction.RETRY_FROM and self.retry_from_step is None:
            raise ValueError("retry_from_step is required when on_failure is retry_from")
        return self


class TaskFilePurpose(StrEnum):
    REASONING = "reasoning"
    RESULT = "result"
    ANALYSIS = "analysis"
    LOG = "log"
    ARTIFACT = "artifact"
    OTHER = "other"


class MultiChoiceOption(CyborgModel):
    value: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    image_url: str | None = Field(default=None, description="Relative path to image in project workspace")
    audio_url: str | None = Field(default=None, description="Relative path to MP3 in project workspace")

    @field_validator("value", "label")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("image_url", "audio_url")
    @classmethod
    def must_be_valid_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if ".." in stripped or stripped.startswith("/"):
            raise ValueError("URL must be a relative path without '..' or leading '/'")
        return stripped


class TextInputSchema(CyborgModel):
    type: Literal["text"] = "text"
    prompt: str = Field(min_length=1, description="Question or prompt to show the user")
    placeholder: str | None = None

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must not be blank")
        return stripped


class MultiChoiceInputSchema(CyborgModel):
    type: Literal["multi_choice"] = "multi_choice"
    prompt: str = Field(min_length=1, description="Question or prompt to show the user")
    options: list[MultiChoiceOption] = Field(min_length=1)
    allow_multiple: bool = False

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must not be blank")
        return stripped


TaskInputSchema = Annotated[TextInputSchema | MultiChoiceInputSchema, Field(discriminator="type")]


class TaskInputResolveRequest(CyborgModel):
    """User's response to a task input request submitted via the dashboard."""
    response: str | list[str]

    @field_validator("response")
    @classmethod
    def response_must_not_be_empty(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("response must not be blank")
            return stripped
        if isinstance(value, list):
            if not value or all(not v.strip() for v in value):
                raise ValueError("response must contain at least one selection")
            return [v.strip() for v in value if v.strip()]
        return value


class TaskTargetSession(CyborgModel):
    channel: Literal["whatsapp"] | None = None
    kind: TaskTargetSessionKind | None = None
    session_key: str | None = None
    chat_id: str | None = None
    contact_id: UUID | None = None

    @field_validator("session_key", "chat_id")
    @classmethod
    def optional_strings_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("target session values must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_target(self) -> "TaskTargetSession":
        # Session-only target (e.g., cyborg:project:X:task:Y) — just needs session_key
        if self.channel is None and self.kind is None and isinstance(self.session_key, str) and self.session_key.strip():
            return self
        # WhatsApp targets require both channel and kind
        if self.channel is None or self.kind is None:
            raise ValueError("target_session requires either session_key alone, or channel and kind")
        if self.kind == TaskTargetSessionKind.GROUP:
            if self.session_key is None and self.chat_id is None:
                raise ValueError("group target_session requires session_key or chat_id")
            if self.contact_id is not None:
                raise ValueError("group target_session cannot include contact_id")
        if self.kind == TaskTargetSessionKind.DM:
            if self.contact_id is None:
                raise ValueError("dm target_session requires contact_id")
            if self.session_key is not None:
                raise ValueError("dm target_session cannot include session_key")
            if self.chat_id is not None:
                raise ValueError("dm target_session cannot include chat_id")
        return self


def _normalize_task_metadata(value: MetadataDict | None) -> MetadataDict | None:
    if value is None:
        return value
    normalized = dict(value)
    target_session = normalized.get("target_session")
    if target_session is None:
        return normalized
    if not isinstance(target_session, dict):
        raise ValueError("metadata.target_session must be an object")
    normalized["target_session"] = TaskTargetSession.model_validate(target_session).model_dump(
        mode="json",
        exclude_none=True,
    )
    return normalized


class EntityRef(BaseModel):
    """Common identity model."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID


class SoftDeleteFields(BaseModel):
    """Soft delete tracking fields."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    deleted_at: datetime | None = None


class TaskFields(CyborgModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    requested_by: str | None = Field(default=None, max_length=200)
    plan: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    parent_id: UUID | None = None
    retry_config: RetryConfig | None = None
    is_recurring: bool = False
    recurrence_rule: str | None = None
    next_run_at: datetime | None = None
    metadata: MetadataDict = Field(default_factory=dict)
    blocked_reason: str | None = None
    blocked_resume_instructions: str | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @field_validator("plan")
    @classmethod
    def plan_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("plan must not be blank")
        return stripped

    @field_validator("recurrence_rule")
    @classmethod
    def validate_recurrence_rule(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return validate_cron_expression(value)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: MetadataDict) -> MetadataDict:
        return _normalize_task_metadata(value) or {}

    @model_validator(mode="after")
    def validate_recurrence(self) -> "TaskFields":
        if self.is_recurring and not self.recurrence_rule:
            raise ValueError("recurrence_rule is required when is_recurring is true")
        if not self.is_recurring and self.recurrence_rule is None and self.next_run_at is not None:
            raise ValueError("next_run_at requires a recurrence rule")
        return self


class TaskCreate(TaskFields):
    plan: str = Field(min_length=1)
    project_ids: list[UUID] = Field(default_factory=list)


class TaskUpdate(CyborgModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    requested_by: str | None = Field(default=None, max_length=200)
    plan: str | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    parent_id: UUID | None = None
    retry_config: RetryConfig | None = None
    is_recurring: bool | None = None
    recurrence_rule: str | None = None
    next_run_at: datetime | None = None
    metadata: MetadataDict | None = None
    project_ids: list[UUID] | None = None
    blocked_reason: str | None = None
    blocked_resume_instructions: str | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @field_validator("recurrence_rule")
    @classmethod
    def validate_recurrence_rule(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return validate_cron_expression(value)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: MetadataDict | None) -> MetadataDict | None:
        return _normalize_task_metadata(value)


class TaskResponse(TaskFields, EntityRef, SoftDeleteFields):
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    result: str | None = None
    project_ids: list[UUID] = Field(default_factory=list)
    blocked_at: datetime | None = None
    notification_count: int = 0
    last_notification_at: datetime | None = None
    needs_input_since: datetime | None = None
    output_directory: str | None = None
    files: list[TaskFileResponse] = Field(default_factory=list)


class TaskStepFields(CyborgModel):
    step_number: int = Field(ge=1)
    description: str = Field(min_length=1)
    status: TaskStepStatus = TaskStepStatus.PENDING
    result: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("description must not be blank")
        return stripped


class TaskStepCreate(TaskStepFields):
    pass


class TaskStepUpdate(CyborgModel):
    description: str | None = None
    status: TaskStepStatus | None = None
    result: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskStepResponse(TaskStepFields, EntityRef):
    task_id: UUID


class TaskHistoryResponse(EntityRef):
    task_id: UUID
    action: str
    details: MetadataDict = Field(default_factory=dict)
    timestamp: datetime


class TaskFailureRequest(CyborgModel):
    details: MetadataDict = Field(default_factory=dict)
    result: str | None = None


class TaskBlockRequest(CyborgModel):
    """Request to block a task waiting for user input.

    Both reason and resume_instructions are required to ensure the task can be
    resumed without relying on conversational context.
    """
    reason: str = Field(min_length=1, description="Why the task is blocked")
    resume_instructions: str = Field(min_length=1, description="Full instructions on how to resume this task when unblocked")
    input_schema: TaskInputSchema | None = Field(default=None, description="Optional structured input request to show in the dashboard")

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped

    @field_validator("resume_instructions")
    @classmethod
    def resume_instructions_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("resume_instructions must not be blank")
        return stripped


class TaskUnblockRequest(CyborgModel):
    """Request to unblock a task and resume work."""
    notes: str | None = Field(default=None, description="Optional notes about why the task is being unblocked")


class TaskRetryRequest(CyborgModel):
    details: MetadataDict = Field(default_factory=dict)


class TaskVerifySubmitRequest(CyborgModel):
    """Request to verify a task submission with a one-time password."""
    otp: str = Field(min_length=1, description="One-time password from the submission review prompt")
    approved: bool
    reason: str | None = Field(default=None, description="Reason for rejection (required when not approved)")
    issues: list[str] | None = Field(default=None, description="Specific issues found during review")


class ProjectDecideNextRequest(CyborgModel):
    """Async response from reasoning with the next action for a project."""
    otp: str = Field(min_length=1, description="One-time password from the next-action prompt")
    action: str = Field(description="One of: create_task, close_project, block_project")
    reasoning: str = Field(default="", description="Why this action was chosen")
    task_title: str | None = Field(default=None, max_length=200)
    task_description: str | None = Field(default=None)
    task_plan: str | None = Field(default=None)
    task_priority: str | None = Field(default="high")
    block_reason: str | None = Field(default=None)
    resume_instructions: str | None = Field(default=None)


class TaskFileCreate(CyborgModel):
    """Request to register a file produced by a task."""
    filename: str = Field(min_length=1, max_length=255)
    purpose: TaskFilePurpose = TaskFilePurpose.ARTIFACT
    description: str | None = None
    content_type: str = "text/plain"

    @field_validator("filename")
    @classmethod
    def filename_must_not_have_path_sep(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("filename must not be blank")
        if "/" in stripped or "\\" in stripped:
            raise ValueError("filename must not contain path separators")
        return stripped


class TaskFileResponse(CyborgModel, EntityRef):
    task_id: UUID
    project_id: UUID
    filename: str
    relative_path: str
    purpose: TaskFilePurpose
    description: str | None = None
    content_type: str = "text/plain"
    size_bytes: int | None = None
    metadata: MetadataDict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TaskFileListResponse(CyborgModel):
    task_id: UUID
    output_directory: str | None = None
    files: list[TaskFileResponse] = Field(default_factory=list)


class ProjectFields(CyborgModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    aim: str | None = None
    method: str | None = None
    state: ProjectState = ProjectState.PLANNING
    conclusion: str | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ProjectCreate(CyborgModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    aim: str | None = None
    method: str | None = None
    state: ProjectState = ProjectState.PLANNING
    conclusion: str | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    task_ids: list[UUID] = Field(default_factory=list)
    metadata: MetadataDict = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ProjectUpdate(CyborgModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    aim: str | None = None
    method: str | None = None
    conclusion: str | None = None
    plan: list[PlanStep] | None = None
    success_criteria: list[SuccessCriterion] | None = None
    task_ids: list[UUID] | None = None
    metadata: MetadataDict | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ProjectResponse(CyborgModel, EntityRef, SoftDeleteFields):
    title: str
    description: str | None = None
    aim: str | None = None
    method: str | None = None
    state: ProjectState
    conclusion: str | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    subagent_session_key: str | None = None
    metadata: MetadataDict = Field(default_factory=dict)
    blocked_reason: str | None = None
    blocked_resume_instructions: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    closed_at: datetime | None = None
    task_ids: list[UUID] = Field(default_factory=list)
    current_spec_id: UUID | None = None
    latest_spec_id: UUID | None = None
    latest_spec_status: ProjectSpecStatus | None = None
    notification_count: int = 0
    last_notification_at: datetime | None = None
    needs_input_since: datetime | None = None
    notifications_muted: bool = False


class ProjectSpecFields(CyborgModel):
    aim: str = Field(min_length=1)
    method: str | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(min_length=1)

    @field_validator("aim")
    @classmethod
    def project_spec_text_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class ProjectSpecSubmitRequest(ProjectSpecFields):
    pass


class ProjectSpecApproveRequest(CyborgModel):
    approver: str = Field(min_length=1, max_length=200)

    @field_validator("approver")
    @classmethod
    def approver_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("approver must not be blank")
        return stripped


class ProjectSpecRejectRequest(CyborgModel):
    feedback: str = Field(min_length=1)

    @field_validator("feedback")
    @classmethod
    def feedback_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("feedback must not be blank")
        return stripped


class ProjectSpecResponse(ProjectSpecFields, EntityRef):
    project_id: UUID
    version_number: int = Field(ge=1)
    status: ProjectSpecStatus
    feedback: str | None = None
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    is_current: bool = False


class ProjectSpecListResponse(CyborgModel):
    project_id: UUID
    specs: list[ProjectSpecResponse]
    current_spec_id: UUID | None = None
    latest_spec_id: UUID | None = None
    latest_spec_status: ProjectSpecStatus | None = None


class ProjectCloseRequest(CyborgModel):
    conclusion: str | None = None


class ProjectJournalEntryFields(CyborgModel):
    entry_type: JournalEntryType
    content: str = Field(min_length=1)
    metadata: MetadataDict = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped


class ProjectJournalEntryCreate(ProjectJournalEntryFields):
    pass


class ProjectJournalEntryResponse(ProjectJournalEntryFields, EntityRef):
    project_id: UUID
    created_at: datetime


class CalendarFields(CyborgModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    color: str | None = None
    is_default: bool = False
    metadata: MetadataDict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not COLOR_PATTERN.match(value):
            raise ValueError("color must be a #RGB or #RRGGBB hex value")
        return value


class CalendarCreate(CalendarFields):
    pass


class CalendarUpdate(CyborgModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    color: str | None = None
    is_default: bool | None = None
    metadata: MetadataDict | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not COLOR_PATTERN.match(value):
            raise ValueError("color must be a #RGB or #RRGGBB hex value")
        return value


class CalendarResponse(CalendarFields, EntityRef, SoftDeleteFields):
    created_at: datetime


class EventFields(CyborgModel):
    calendar_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    agenda: str | None = None
    venue: str | None = None
    start_time: datetime
    end_time: datetime
    timezone: str = "UTC"
    is_all_day: bool = False
    recurrence_rule: str | None = None
    status: EventStatus = EventStatus.TENTATIVE

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_window(self) -> "EventFields":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class EventCreate(EventFields):
    pass


class EventUpdate(CyborgModel):
    calendar_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    agenda: str | None = None
    venue: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    timezone: str | None = None
    is_all_day: bool | None = None
    recurrence_rule: str | None = None
    status: EventStatus | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_window(self) -> "EventUpdate":
        if self.start_time is not None and self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class EventResponse(EventFields, EntityRef, SoftDeleteFields):
    created_at: datetime
    updated_at: datetime


class EventRecipientFields(CyborgModel):
    recipient_type: RecipientType
    recipient_address: str = Field(min_length=1)
    name: str | None = None
    status: RecipientStatus = RecipientStatus.PENDING
    responded_at: datetime | None = None
    notes: str | None = None

    @field_validator("recipient_address")
    @classmethod
    def address_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("recipient_address must not be blank")
        return stripped


class EventRecipientCreate(EventRecipientFields):
    pass


class EventRecipientUpdate(CyborgModel):
    recipient_type: RecipientType | None = None
    recipient_address: str | None = None
    name: str | None = None
    status: RecipientStatus | None = None
    responded_at: datetime | None = None
    notes: str | None = None

    @field_validator("recipient_address")
    @classmethod
    def address_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("recipient_address must not be blank")
        return stripped


class EventRecipientResponse(EventRecipientFields, EntityRef):
    event_id: UUID


class TaskContextItem(CyborgModel):
    id: UUID
    title: str
    status: TaskStatus
    priority: TaskPriority
    updated_at: datetime
    parent_project_id: UUID | None = None
    parent_project_title: str | None = None


class ProjectContextItem(CyborgModel):
    id: UUID
    title: str
    state: ProjectState
    aim: str | None = None


class EventContextItem(CyborgModel):
    id: UUID
    title: str
    start_time: datetime
    end_time: datetime
    timezone: str
    status: EventStatus
    venue: str | None = None
    calendar_id: UUID


class ContextSummaryResponse(CyborgModel):
    generated_at: datetime
    task_counts: dict[str, int]
    project_counts: dict[str, int]
    upcoming_events: list[EventContextItem]
    active_tasks: list[TaskContextItem]
    active_projects: list[ProjectContextItem]


class ContextTasksResponse(CyborgModel):
    generated_at: datetime
    tasks: list[TaskContextItem]


class ContextProjectsResponse(CyborgModel):
    generated_at: datetime
    projects: list[ProjectContextItem]


class ContextCalendarResponse(CyborgModel):
    generated_at: datetime
    events: list[EventContextItem]


# Contact models


class ContactFields(CyborgModel):
    name: str = Field(min_length=1, max_length=255)
    phone_number: str = Field(min_length=1, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    whatsapp_groups: list[str] = Field(default_factory=list)
    metadata: MetadataDict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("phone_number")
    @classmethod
    def phone_number_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("phone_number must not be blank")
        return stripped


class ContactCreate(ContactFields):
    pass


class ContactUpdate(CyborgModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone_number: str | None = Field(default=None, min_length=1, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    whatsapp_groups: list[str] | None = None
    metadata: MetadataDict | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("phone_number")
    @classmethod
    def phone_number_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("phone_number must not be blank")
        return stripped


class ContactResponse(ContactFields, EntityRef, SoftDeleteFields):
    created_at: datetime
    updated_at: datetime


class SessionRouteFields(CyborgModel):
    channel: Literal["whatsapp"]
    session_key: str = Field(min_length=1, max_length=255)
    kind: SessionRouteKind
    chat_id: str | None = None
    contact_id: UUID | None = None
    metadata: MetadataDict = Field(default_factory=dict)

    @field_validator("session_key", "chat_id")
    @classmethod
    def route_strings_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("route values must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_route_shape(self) -> "SessionRouteFields":
        if self.kind == SessionRouteKind.GROUP:
            if self.chat_id is None:
                raise ValueError("group session routes require chat_id")
            if self.contact_id is not None:
                raise ValueError("group session routes cannot include contact_id")
        if self.kind == SessionRouteKind.DM:
            if self.contact_id is None:
                raise ValueError("dm session routes require contact_id")
            if self.chat_id is not None:
                raise ValueError("dm session routes cannot include chat_id")
        return self


class SessionRouteCreate(SessionRouteFields):
    pass


class SessionRouteUpdate(CyborgModel):
    chat_id: str | None = None
    contact_id: UUID | None = None
    metadata: MetadataDict | None = None
    is_active: bool | None = None

    @field_validator("chat_id")
    @classmethod
    def update_chat_id_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("chat_id must not be blank")
        return stripped


class SessionRouteResponse(SessionRouteFields, EntityRef, SoftDeleteFields):
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class ResolvedSessionRoute(CyborgModel):
    channel: Literal["whatsapp"]
    kind: SessionRouteKind
    to: str
    session_key: str | None = None
    chat_id: str | None = None
    contact_id: UUID | None = None
    contact_name: str | None = None
    phone_number: str | None = None
    route_source: str | None = None
    metadata: MetadataDict = Field(default_factory=dict)


class NotificationResponse(CyborgModel, EntityRef):
    entity_type: NotificationEntityType
    entity_id: UUID
    notification_type: NotificationType
    status: NotificationStatus
    delivery_status: NotificationDeliveryStatus = NotificationDeliveryStatus.PENDING
    delivery_attempt_count: int = 0
    last_delivery_at: datetime | None = None
    last_delivery_error: str | None = None
    next_delivery_at: datetime | None = None
    title: str
    message: str
    metadata: MetadataDict = Field(default_factory=dict)
    sequence_number: int | None = None
    created_at: datetime
    updated_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None
    source_updated_at: str | None = None


class NotificationAcknowledgeRequest(CyborgModel):
    acknowledged_by: str | None = Field(default=None, max_length=200)

    @field_validator("acknowledged_by")
    @classmethod
    def acknowledged_by_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("acknowledged_by must not be blank")
        return stripped


class HealthResponse(CyborgModel):
    status: Literal["ok"]
    database: Literal["ok"]
