import re
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.models import Priority, Provider

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _timezone(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown timezone") from None
    return value


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.match(value):
            raise ValueError("Некорректный email")
        return value


class RegisterRequest(Credentials):
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=200)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return _timezone(value)


class LoginRequest(Credentials):
    pass


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str | None = None
    timezone: str | None = None
    telegram_username: str | None = None
    telegram_linked_at: datetime | None = None


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return _timezone(value)


class CalendarCreate(BaseModel):
    name: str
    provider: str = "local"
    integration_id: int | None = None
    description: str | None = None
    timezone: str = "UTC"


class CalendarRead(CalendarCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    external_id: str | None = None
    is_active: bool


class IntegrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_email: str | None = None
    status: str
    last_sync_at: datetime | None = None
    last_sync_error: str | None = None
    config: dict = Field(default_factory=dict)


class IntegrationConnect(BaseModel):
    values: dict = Field(default_factory=dict, max_length=20)
    # Same-site path to come back to after an OAuth provider (e.g. the onboarding step)
    return_to: str | None = Field(default=None, max_length=300)


class EventLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_id: int
    integration_id: int
    external_id: str
    url: str | None = None


class EventCreate(BaseModel):
    calendar_id: int | None = None
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"
    priority: Priority = Priority.MEDIUM
    location: str | None = Field(default=None, max_length=500)
    all_day: bool = False
    reminder_minutes: int | None = Field(default=None, ge=0, le=10080)


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = None
    priority: Priority | None = None
    location: str | None = Field(default=None, max_length=500)
    all_day: bool | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=10080)


class EventRead(EventCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    calendar_id: int
    user_id: int
    status: str
    source: str
    sync_status: str
    external_id: str | None = None


class AssistantMessage(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    timezone: str = "Europe/Moscow"


class AssistantResponse(BaseModel):
    answer: str | None = None
    created_events: list[EventRead] = Field(default_factory=list)
    proposed_events: list[dict] = Field(default_factory=list)


class AssistantConfirmation(BaseModel):
    events: list[dict] = Field(max_length=50)
    timezone: str = "Europe/Moscow"


def _lead_times(value: list[int]) -> list[int]:
    if any(minutes < 0 or minutes > 10080 for minutes in value):
        raise ValueError("Lead time must be between 0 and 10080 minutes")
    return sorted(set(value))


class ReminderSettingsBase(BaseModel):
    enabled: bool = True
    lead_times: list[int] = Field(default_factory=lambda: [15], max_length=10)
    daily_digest_enabled: bool = False
    daily_digest_time: time = time(9, 0)
    quiet_hours_enabled: bool = False
    quiet_hours_start: time = time(23, 0)
    quiet_hours_end: time = time(8, 0)
    sources: list[Provider] = Field(default_factory=list)

    @field_validator("lead_times")
    @classmethod
    def check_lead_times(cls, value: list[int]) -> list[int]:
        return _lead_times(value)


class ReminderSettingsRead(ReminderSettingsBase):
    model_config = ConfigDict(from_attributes=True)


class ReminderSettingsUpdate(BaseModel):
    enabled: bool | None = None
    lead_times: list[int] | None = Field(default=None, max_length=10)
    daily_digest_enabled: bool | None = None
    daily_digest_time: time | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    sources: list[Provider] | None = None

    @field_validator("lead_times")
    @classmethod
    def check_lead_times(cls, value: list[int] | None) -> list[int] | None:
        return None if value is None else _lead_times(value)


class TelegramStatus(BaseModel):
    linked: bool
    username: str | None = None
    linked_at: datetime | None = None
    bot_username: str | None = None


class TelegramLinkResponse(BaseModel):
    code: str
    expires_at: datetime
    deep_link: str | None = None


class BotLinkRequest(BaseModel):
    code: str = Field(min_length=8, max_length=64)
    chat_id: int
    username: str | None = Field(default=None, max_length=64)


class BotClaimRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


class BotAckRequest(BaseModel):
    ok: bool
    error: str | None = None
    chat_unreachable: bool = False


class BotChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class BotUndoRequest(BaseModel):
    event_ids: list[int] = Field(min_length=1, max_length=50)


class BotSnoozeRequest(BaseModel):
    chat_id: int
    minutes: int = Field(ge=5, le=1440)
