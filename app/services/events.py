import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Calendar, Event, Integration, User
from app.services.google_calendar import GoogleCalendarProvider
from app.services.integrations.google import google_event_body
from app.services.integrations.service import event_payload, integration_secrets, store_secrets


async def default_calendar(session: AsyncSession, user: User, timezone_name: str) -> Calendar:
    calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user.id, Calendar.provider == "local").order_by(Calendar.id)
    )
    if not calendar:
        calendar = Calendar(user_id=user.id, name="Личный календарь", provider="local", timezone=timezone_name)
        session.add(calendar)
        await session.flush()
    return calendar


async def google_provider(session: AsyncSession, user_id: int) -> tuple[Integration | None, GoogleCalendarProvider | None]:
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "google")
    )
    secrets = integration_secrets(integration) if integration else {}
    if not secrets.get("access_token"):
        return None, None
    return integration, GoogleCalendarProvider(secrets["access_token"], secrets.get("refresh_token"))


def remember_google_token(integration: Integration, provider: GoogleCalendarProvider) -> None:
    store_secrets(integration, {"access_token": provider.access_token})


async def push_new_events_to_google(session: AsyncSession, user_id: int, events: list[Event]) -> None:
    integration, provider = await google_provider(session, user_id)
    if not provider or not events:
        return
    for event in events:
        try:
            result = await provider.create_event("primary", google_event_body(event_payload(event)))
            event.external_id = result.get("id")
            event.sync_status = "synced"
            event.source = "google"
        except httpx.HTTPError:
            event.sync_status = "error"
    remember_google_token(integration, provider)
    await session.commit()
    for event in events:
        await session.refresh(event)
