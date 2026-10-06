from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_json, encrypt_json
from app.models.models import Calendar, Event, EventLink, Integration, SyncStatus, User
from app.services.integrations.base import EventPayload, IntegrationError, IntegrationProvider, ProviderContext
from app.services.integrations.registry import PROVIDERS

SYNC_PAST = timedelta(days=7)
SYNC_FUTURE = timedelta(days=60)


def user_timezone(user: User) -> str:
    try:
        return str(ZoneInfo(user.timezone)) if user.timezone else settings.default_timezone
    except (ZoneInfoNotFoundError, ValueError):
        return settings.default_timezone


def integration_secrets(integration: Integration) -> dict:
    return decrypt_json(integration.credentials_encrypted)


def build_provider(integration: Integration, tz: str) -> IntegrationProvider:
    provider_class = PROVIDERS.get(integration.provider)
    if not provider_class:
        raise IntegrationError(f"Неизвестный сервис {integration.provider}")
    return provider_class(ProviderContext(config=integration.config or {}, secrets=integration_secrets(integration), timezone=tz))


def store_secrets(integration: Integration, secrets: dict) -> None:
    integration.credentials_encrypted = encrypt_json({**integration_secrets(integration), **secrets})


def _persist_rotated_secrets(integration: Integration, provider: IntegrationProvider) -> None:
    if provider.context.updated_secrets:
        store_secrets(integration, provider.context.updated_secrets)


def event_payload(event: Event) -> EventPayload:
    return EventPayload(
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        timezone=event.timezone or settings.default_timezone,
        all_day=event.all_day,
        description=event.description,
        location=event.location,
    )


async def import_calendar(session: AsyncSession, user: User, integration: Integration) -> Calendar:
    calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user.id, Calendar.provider == integration.provider, Calendar.external_id == "primary")
    )
    if not calendar:
        calendar = Calendar(
            user_id=user.id,
            integration_id=integration.id,
            name=PROVIDERS[integration.provider].title,
            provider=integration.provider,
            external_id="primary",
            timezone=user_timezone(user),
        )
        session.add(calendar)
        await session.flush()
    elif calendar.integration_id != integration.id:
        calendar.integration_id = integration.id
    return calendar


async def verify_integration(session: AsyncSession, user: User, integration: Integration) -> str:
    provider = build_provider(integration, user_timezone(user))
    try:
        account = await provider.verify()
    except IntegrationError as error:
        integration.status = "error"
        integration.last_sync_error = str(error)
        await session.commit()
        raise
    _persist_rotated_secrets(integration, provider)
    integration.account_email = account[:320]
    integration.status = "connected"
    integration.last_sync_error = None
    await session.commit()
    return account


async def sync_integration(session: AsyncSession, user: User, integration: Integration) -> dict:
    tz = user_timezone(user)
    provider = build_provider(integration, tz)
    now = datetime.now(timezone.utc)
    try:
        items = await provider.fetch_items(now - SYNC_PAST, now + SYNC_FUTURE)
    except IntegrationError as error:
        integration.status = "error"
        integration.last_sync_error = str(error)
        await session.commit()
        raise
    _persist_rotated_secrets(integration, provider)
    calendar = await import_calendar(session, user, integration)
    exported = set(await session.scalars(select(EventLink.external_id).where(EventLink.integration_id == integration.id)))
    created = updated = skipped = 0
    for item in items:
        if item.external_id in exported:
            skipped += 1
            continue
        external_id = f"{integration.provider}:{integration.id}:{item.external_id}" if provider.scoped_external_ids else item.external_id
        external_id = external_id[:255]
        event = await session.scalar(select(Event).where(Event.external_id == external_id))
        if event and event.user_id != user.id:
            skipped += 1
            continue
        if not event:
            event = Event(calendar_id=calendar.id, user_id=user.id, external_id=external_id, source=integration.provider)
            session.add(event)
            created += 1
        else:
            updated += 1
        event.title = item.title[:300]
        event.description = "\n".join(part for part in (item.description, item.url) if part) or None
        event.location = item.location[:500] if item.location else None
        event.start_at = item.start_at
        event.end_at = item.end_at if item.end_at > item.start_at else item.start_at + timedelta(minutes=30)
        event.all_day = item.all_day
        event.timezone = tz
        event.sync_status = SyncStatus.SYNCED
    integration.status = "connected"
    integration.last_sync_at = now
    integration.last_sync_error = None
    await session.commit()
    return {"fetched": len(items), "created": created, "updated": updated, "skipped": skipped}


async def push_event(session: AsyncSession, user: User, integration: Integration, event: Event) -> EventLink:
    provider = build_provider(integration, user_timezone(user))
    result = await provider.push_event(event_payload(event))
    _persist_rotated_secrets(integration, provider)
    link = EventLink(event_id=event.id, integration_id=integration.id, external_id=result.external_id[:255], url=result.url)
    session.add(link)
    await session.commit()
    await session.refresh(link)
    return link
