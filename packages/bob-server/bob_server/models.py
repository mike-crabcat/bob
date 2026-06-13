"""Pydantic models for the Bob data service."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MetadataDict = dict[str, Any]
COLOR_PATTERN = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


class BobModel(BaseModel):
    """Shared model configuration."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SessionRouteKind(StrEnum):
    GROUP = "group"
    DM = "dm"
    THREAD = "thread"


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


class RetryConfig(BobModel):
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


class EntityRef(BaseModel):
    """Common identity model."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID


class SoftDeleteFields(BaseModel):
    """Soft delete tracking fields."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    deleted_at: datetime | None = None


class CalendarFields(BobModel):
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


class CalendarUpdate(BobModel):
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


class EventFields(BobModel):
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


class EventUpdate(BobModel):
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


class EventRecipientFields(BobModel):
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


class EventRecipientUpdate(BobModel):
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


class EventContextItem(BobModel):
    id: UUID
    title: str
    start_time: datetime
    end_time: datetime
    timezone: str
    status: EventStatus
    venue: str | None = None
    calendar_id: UUID


class ContextSummaryResponse(BobModel):
    generated_at: datetime
    upcoming_events: list[EventContextItem]


class ContextCalendarResponse(BobModel):
    generated_at: datetime
    events: list[EventContextItem]


# Contact models


class ContactFields(BobModel):
    name: str = Field(min_length=1, max_length=255)
    phone_number: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
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
    def phone_number_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return stripped


class ContactCreate(ContactFields):
    pass


class ContactUpdate(BobModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone_number: str | None = Field(default=None, min_length=1, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    is_trusted: bool | None = None
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
    is_trusted: bool = False


class SessionRouteFields(BobModel):
    channel: Literal["whatsapp", "email", "phone"]
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
        if self.kind == SessionRouteKind.THREAD:
            if self.chat_id is None:
                raise ValueError("thread session routes require chat_id")
            if self.contact_id is not None:
                raise ValueError("thread session routes cannot include contact_id")
        return self


class SessionRouteCreate(SessionRouteFields):
    pass


class SessionRouteUpdate(BobModel):
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


class ResolvedSessionRoute(BobModel):
    channel: Literal["whatsapp", "email", "phone"] | None = None
    kind: SessionRouteKind | None = None
    to: str | None = None
    session_key: str | None = None
    chat_id: str | None = None
    contact_id: UUID | None = None
    contact_name: str | None = None
    phone_number: str | None = None
    route_source: str | None = None
    metadata: MetadataDict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Email relay models
# ---------------------------------------------------------------------------


class EmailInboxCreate(BobModel):
    agentmail_inbox_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=200)
    email_address: str = Field(min_length=1)
    metadata: MetadataDict = Field(default_factory=dict)

    @field_validator("email_address")
    @classmethod
    def email_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("email_address must not be blank")
        return stripped


class EmailInboxUpdate(BobModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None
    metadata: MetadataDict | None = None


class EmailInboxResponse(BobModel, EntityRef):
    agentmail_inbox_id: str
    display_name: str
    email_address: str
    is_active: bool = True
    last_polled_at: datetime | None = None
    metadata: MetadataDict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class EmailAttachment(BobModel):
    content: str = Field(min_length=1, description="Base64-encoded file content")
    filename: str | None = Field(default=None, max_length=255)
    content_type: str | None = Field(default=None)
    content_id: str | None = Field(default=None, description="CID for inline images")
    content_disposition: str | None = Field(default=None, pattern=r"^(inline|attachment)$")


class EmailSendRequest(BobModel):
    to: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    text: str = Field(min_length=1)
    html: str | None = None
    cc: list[str] | None = None
    agenda: str = Field(min_length=1, description="Purpose and handling instructions for this email thread")
    attachments: list[EmailAttachment] | None = None


class EmailReplyRequest(BobModel):
    message_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    html: str | None = None
    reply_all: bool = False
    attachments: list[EmailAttachment] | None = None


class EmailThreadResponse(BobModel, EntityRef):
    inbox_id: UUID
    agentmail_thread_id: str
    subject: str | None = None
    contact_id: UUID | None = None
    project_id: UUID | None = None
    session_key: str
    agenda: str | None = None
    message_count: int = 0
    last_message_at: datetime | None = None
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class HealthResponse(BobModel):
    status: Literal["ok"]
    database: Literal["ok"]


class PersonaConfig(BobModel):
    owner_name: str = "Mike"
    model: str = "OpenAI 5.4 mini"
    channel: str = "WhatsApp"
    host: str = "mike-workstation"


class PersonaRecord(BobModel):
    id: str
    revision: int
    soul: str
    identity: str
    agents: str
    user_content: str
    config: PersonaConfig
    is_active: bool
    created_at: str


class PersonaUpdate(BobModel):
    soul: str
    identity: str
    agents: str
    user_content: str
    config: PersonaConfig
