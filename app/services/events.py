import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Calendar, Event, Integration, User
from app.services.google_calendar import GoogleCalendarProvider
from app.services.integrations.google import google_event_body
from app.services.integrations.service import event_payload, integration_secrets, store_secrets

logger = logging.getLogger(__name__)


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


GOOGLE_ACCESS_LOST = "Доступ к Google Calendar истёк — подключите его заново"


def access_lost(error: httpx.HTTPError) -> bool:
    """Google refused the refresh token (revoked, or a week old while the Google app is in testing)."""
    return isinstance(error, httpx.HTTPStatusError) and error.request.url.host == "oauth2.googleapis.com"


def mark_access_lost(integration: Integration) -> None:
    integration.status = "error"
    integration.last_sync_error = GOOGLE_ACCESS_LOST


def remember_google_token(integration: Integration, provider: GoogleCalendarProvider) -> None:
    store_secrets(integration, {"access_token": provider.access_token})


async def push_new_events_to_google(session: AsyncSession, user_id: int, events: list[Event]) -> None:
    integration, provider = await google_provider(session, user_id)
    if not events:
        return
    if not provider:
        if integration:
            # Connected once, but there is no access to use: the reply must not say the tasks went to Google
            logger.warning("Google is connected for user %s without an access token", user_id)
            mark_access_lost(integration)
            for event in events:
                event.sync_status = "error"
            await session.commit()
        return
    for event in events:
        try:
            result = await provider.create_event("primary", google_event_body(event_payload(event)))
            event.external_id = result.get("id")
            event.sync_status = "synced"
            event.source = "google"
            logger.info("Event %s of user %s created in Google as %s", event.id, user_id, event.external_id)
        except httpx.HTTPError as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            logger.warning("Google did not accept event %s of user %s: %s", event.id, user_id, status or error)
            event.sync_status = "error"
            if access_lost(error):
                mark_access_lost(integration)
                for rest in events:
                    rest.sync_status = "error" if rest.sync_status != "synced" else rest.sync_status
                break
    remember_google_token(integration, provider)
    await session.commit()
    for event in events:
        await session.refresh(event)


async def push_pending_to_google(session: AsyncSession, user_id: int) -> None:
    """Send the Google events changed in Dayla (moved, renamed, new time) back to Google Calendar.

    Without it the next import brings Google's old version back and the change is lost. An event Google
    refused stays "pending": the import leaves it alone and the next change or sync tries again."""
    events = list(
        await session.scalars(
            select(Event).where(Event.user_id == user_id, Event.source == "google", Event.external_id.is_not(None), Event.sync_status == "pending")
        )
    )
    if not events:
        return
    integration, provider = await google_provider(session, user_id)
    if not provider:
        return
    for event in events:
        body = google_event_body(event_payload(event))
        try:
            await provider.patch_event("primary", event.external_id, {key: body[key] for key in ("summary", "start", "end")})
            event.sync_status = "synced"
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (404, 410):
                # Deleted in Google: the import no longer brings it back, the Dayla copy keeps the change
                event.sync_status = "error"
            logger.warning("Google refused the change of event %s: %s", event.id, error.response.status_code)
        except httpx.HTTPError:
            logger.warning("Could not send the change of event %s to Google", event.id)
    remember_google_token(integration, provider)
    await session.commit()
