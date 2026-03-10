"""Pydantic models for the Cyborg data service."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Literal
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

    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


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


class EntityRef(CyborgModel):
    """Common identity model."""

    id: UUID


class SoftDeleteFields(CyborgModel):
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

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
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

    @model_validator(mode="after")
    def validate_recurrence(self) -> "TaskFields":
        if self.is_recurring and not self.recurrence_rule:
            raise ValueError("recurrence_rule is required when is_recurring is true")
        if not self.is_recurring and self.recurrence_rule is None and self.next_run_at is not None:
            raise ValueError("next_run_at requires a recurrence rule")
        return self


class TaskCreate(TaskFields):
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


class TaskResponse(TaskFields, EntityRef, SoftDeleteFields):
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    project_ids: list[UUID] = Field(default_factory=list)


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


class TaskRetryRequest(CyborgModel):
    details: MetadataDict = Field(default_factory=dict)


class ProjectFields(CyborgModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    aim: str | None = None
    state: ProjectState = ProjectState.PLANNING
    conclusion: str | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ProjectCreate(ProjectFields):
    task_ids: list[UUID] = Field(default_factory=list)


class ProjectUpdate(CyborgModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    aim: str | None = None
    state: ProjectState | None = None
    conclusion: str | None = None
    task_ids: list[UUID] | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ProjectResponse(ProjectFields, EntityRef, SoftDeleteFields):
    created_at: datetime
    started_at: datetime | None = None
    paused_at: datetime | None = None
    closed_at: datetime | None = None
    task_ids: list[UUID] = Field(default_factory=list)


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


class HealthResponse(CyborgModel):
    status: Literal["ok"]
    database: Literal["ok"]
