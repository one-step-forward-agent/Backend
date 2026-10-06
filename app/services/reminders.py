import html
import secrets
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, bindparam, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dataenc import email_lookup
from app.models.models import Event, Notification, NotificationStatus, ReminderSettings, User
from app.services import insights, tasks
from app.services.events import default_calendar
from app.services.integrations.service import user_timezone
from app.services.ru import MONTHS, WEEKDAYS_ACCUSATIVE, plural, relative_day, time_range

MAX_LEAD_MINUTES = 7 * 24 * 60
DIGEST_WINDOW = timedelta(hours=6)
CHECKIN_WINDOW = timedelta(hours=3)
CLAIM_TIMEOUT = timedelta(minutes=5)
MAX_ATTEMPTS = 3
LINK_CODE_LIFETIME = timedelta(minutes=15)

LEGACY_EVENTS = text(
    "UPDATE events SET user_id = :user_id, calendar_id = :calendar_id, start_at = starts_at, "
    "end_at = COALESCE(NULLIF(ends_at, starts_at), starts_at + interval '1 hour'), timezone = :timezone, "
    "status = 'confirmed', priority = 'medium', source = 'ai', sync_status = 'not_synced' "
    "WHERE user_id = :chat_id AND start_at IS NULL AND starts_at IS NOT NULL"
)
LEGACY_CONVERSATION = text("UPDATE conversation_messages SET user_id = :user_id WHERE user_id = :chat_id")
# Placeholder users of the old bot had the email "<chat id>@local.invalid"; emails are encrypted, so match its hash
LEGACY_PLACEHOLDER_USER = text(
    "DELETE FROM users WHERE tg_id = :chat_id AND id <> :user_id AND password_hash IS NULL AND email_hash IN :hashes"
).bindparams(bindparam("hashes", expanding=True))


async def get_settings(session: AsyncSession, user: User) -> ReminderSettings:
    reminder_settings = await session.get(ReminderSettings, user.id)
    if not reminder_settings:
        reminder_settings = ReminderSettings(user_id=user.id, enabled=True, lead_times=[15], sources=[])
        session.add(reminder_settings)
        await session.flush()
    return reminder_settings


def apply_settings(reminder_settings: ReminderSettings, values: dict) -> None:
    for key, value in values.items():
        if value is not None:
            setattr(reminder_settings, key, [str(item) for item in value] if key == "sources" else value)


def format_lead(minutes: int) -> str:
    if minutes == 0:
        return "в момент начала"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} дн" if days else "", f"{hours} ч" if hours else "", f"{mins} мин" if mins else ""]
    return " ".join(part for part in parts if part)


def in_quiet_hours(reminder_settings: ReminderSettings, moment: time) -> bool:
    start, end = reminder_settings.quiet_hours_start, reminder_settings.quiet_hours_end
    if start == end:
        return False
    return start <= moment < end if start < end else moment >= start or moment < end


def reminder_text(event: Event, minutes: int, tz: ZoneInfo) -> str:
    starts = event.start_at.astimezone(tz)
    ends = event.end_at.astimezone(tz)
    today = datetime.now(tz).date()
    lead = "Уже идёт" if minutes < 0 else "Начинается сейчас" if minutes == 0 else f"Через {format_lead(minutes)}"
    when = "весь день" if event.all_day else time_range(starts, ends)
    lines = [f"🔔 <b>{lead}</b>", "", f"<b>{html.escape(event.title)}</b>", f"🕒 {when} · {relative_day(starts.date(), today)}"]
    if event.location:
        lines.append(f"📍 {html.escape(event.location)}")
    return "\n".join(lines)


def digest_text(events: list[Event], day: datetime, tz: ZoneInfo) -> str:
    date_text = f"{WEEKDAYS_ACCUSATIVE[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"
    if not events:
        return f"☀️ <b>Доброе утро!</b>\n\nНа {date_text} ничего не запланировано — день свободен 🌿"
    count = len(events)
    lines = ["☀️ <b>Доброе утро!</b>", f"План на {date_text} — {count} {plural(count, 'событие', 'события', 'событий')}", ""]
    for event in events:
        when = "без времени" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
        line = f"<b>{when}</b>  {html.escape(event.title)}"
        if event.location:
            line += f"  · 📍 {html.escape(event.location)}"
        lines.append(line)
    lines += ["", "Хорошего дня ✨"]
    return "\n".join(lines)


async def generate_due(session: AsyncSession, now: datetime) -> int:
    rows = await session.execute(
        select(User, ReminderSettings)
        .join(ReminderSettings, ReminderSettings.user_id == User.id)
        .where(User.telegram_chat_id.is_not(None), User.is_active.is_(True), ReminderSettings.enabled.is_(True))
    )
    values = []
    for user, reminder_settings in rows.all():
        tz = ZoneInfo(user_timezone(user))
        leads = sorted({minutes for minutes in reminder_settings.lead_times or [] if 0 <= minutes <= MAX_LEAD_MINUTES})
        conditions = [Event.user_id == user.id, Event.start_at > now, Event.all_day.is_(False), Event.completed_at.is_(None)]
        conditions.append(
            or_(
                Event.start_at <= now + timedelta(minutes=max(leads, default=0)),
                and_(Event.reminder_minutes.is_not(None), Event.start_at <= now + timedelta(minutes=MAX_LEAD_MINUTES)),
            )
        )
        if reminder_settings.sources:
            conditions.append(Event.source.in_(reminder_settings.sources))
        for event in await session.scalars(select(Event).where(*conditions)):
            event_leads = [event.reminder_minutes] if event.reminder_minutes is not None else leads
            due = [minutes for minutes in event_leads if event.start_at - timedelta(minutes=minutes) <= now]
            if not due:
                continue
            minutes = min(due)
            values.append(
                {
                    "user_id": user.id,
                    "event_id": event.id,
                    "kind": "reminder",
                    "dedupe_key": f"reminder:{event.id}:{int(event.start_at.timestamp())}:{minutes}",
                    "text": reminder_text(event, max(0, round((event.start_at - now).total_seconds() / 60)), tz),
                    "scheduled_for": now,
                    "expires_at": event.start_at,
                }
            )

        if reminder_settings.daily_digest_enabled:
            local_now = now.astimezone(tz)
            digest_at = datetime.combine(local_now.date(), reminder_settings.daily_digest_time, tz)
            if digest_at <= local_now < digest_at + DIGEST_WINDOW:
                day_start = datetime.combine(local_now.date(), time.min, tz)
                day_end = day_start + timedelta(days=1)
                digest_conditions = [Event.user_id == user.id, Event.start_at < day_end, Event.end_at > day_start]
                if reminder_settings.sources:
                    digest_conditions.append(Event.source.in_(reminder_settings.sources))
                events = list(await session.scalars(select(Event).where(*digest_conditions).order_by(Event.all_day.desc(), Event.start_at)))
                values.append(
                    {
                        "user_id": user.id,
                        "event_id": None,
                        "kind": "digest",
                        "dedupe_key": f"digest:{user.id}:{local_now.date().isoformat()}",
                        "text": digest_text(events, local_now, tz),
                        "scheduled_for": now,
                        "expires_at": day_end,
                    }
                )

        if reminder_settings.checkin_enabled:
            local_now = now.astimezone(tz)
            checkin_at = datetime.combine(local_now.date(), reminder_settings.checkin_time, tz)
            dedupe_key = f"checkin:{user.id}:{local_now.date().isoformat()}"
            if checkin_at <= local_now < checkin_at + CHECKIN_WINDOW and not await session.scalar(select(Notification.id).where(Notification.dedupe_key == dedupe_key)):
                found = await insights.checkin(session, user, local_now)
                if found:
                    text_body, payload = found
                    values.append(
                        {
                            "user_id": user.id,
                            "event_id": None,
                            "kind": "checkin",
                            "dedupe_key": dedupe_key,
                            "text": text_body,
                            "payload": payload,
                            "scheduled_for": now,
                            "expires_at": datetime.combine(local_now.date() + timedelta(days=1), time.min, tz),
                        }
                    )
    if not values:
        return 0
    for value in values:
        value.setdefault("payload", None)
    result = await session.execute(insert(Notification).values(values).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    return result.rowcount or 0


async def claim(session: AsyncSession, limit: int = 50) -> list[dict]:
    now = datetime.now(timezone.utc)
    await tasks.extend_series(session, now)
    await generate_due(session, now)
    await session.execute(
        update(Notification)
        .where(Notification.status == NotificationStatus.SENDING, Notification.claimed_at < now - CLAIM_TIMEOUT)
        .values(status=NotificationStatus.PENDING)
    )
    rows = await session.execute(
        select(Notification, User, ReminderSettings)
        .join(User, User.id == Notification.user_id)
        .outerjoin(ReminderSettings, ReminderSettings.user_id == User.id)
        .where(Notification.status == NotificationStatus.PENDING, Notification.scheduled_for <= now)
        .order_by(Notification.scheduled_for, Notification.id)
        .limit(limit * 4)
        .with_for_update(skip_locked=True, of=Notification)
    )
    claimed = []
    for notification, user, reminder_settings in rows.all():
        if len(claimed) >= limit:
            break
        if not user.telegram_chat_id:
            notification.status = NotificationStatus.FAILED
            notification.error = "Telegram is not linked"
            continue
        if notification.expires_at and notification.expires_at <= now:
            notification.status = NotificationStatus.EXPIRED
            continue
        local_time = now.astimezone(ZoneInfo(user_timezone(user))).time()
        if notification.kind != "test" and reminder_settings and reminder_settings.quiet_hours_enabled and in_quiet_hours(reminder_settings, local_time):
            continue
        notification.status = NotificationStatus.SENDING
        notification.claimed_at = now
        notification.attempts += 1
        claimed.append(
            {
                "id": notification.id,
                "chat_id": user.telegram_chat_id,
                "text": notification.text,
                "kind": notification.kind,
                "event_id": notification.event_id,
                "payload": notification.payload,
                "url": f"{settings.public_app_url}/app/events/{notification.event_id}" if settings.public_app_url and notification.event_id else None,
            }
        )
    await session.commit()
    return claimed


async def acknowledge(session: AsyncSession, notification: Notification, ok: bool, error: str | None, chat_unreachable: bool) -> None:
    now = datetime.now(timezone.utc)
    if ok:
        notification.status = NotificationStatus.SENT
        notification.sent_at = now
        notification.error = None
    else:
        notification.error = (error or "unknown error")[:2000]
        if chat_unreachable or notification.attempts >= MAX_ATTEMPTS:
            notification.status = NotificationStatus.FAILED
        else:
            notification.status = NotificationStatus.PENDING
            notification.scheduled_for = now + timedelta(minutes=notification.attempts)
        if chat_unreachable:
            user = await session.get(User, notification.user_id)
            if user:
                user.telegram_chat_id = None
                user.telegram_username = None
    await session.commit()


async def queue_test(session: AsyncSession, user: User) -> Notification:
    reminder_settings = await get_settings(session, user)
    leads = ", ".join(format_lead(minutes) for minutes in sorted(reminder_settings.lead_times or [])) or "не выбраны"
    state = "включены" if reminder_settings.enabled else "выключены"
    now = datetime.now(timezone.utc)
    notification = Notification(
        user_id=user.id,
        kind="test",
        dedupe_key=f"test:{user.id}:{secrets.token_hex(8)}",
        text=f"✅ Тестовое уведомление Dayla.\nНапоминания {state}, за: {leads}.",
        scheduled_for=now,
        expires_at=now + timedelta(hours=1),
        status=NotificationStatus.PENDING,
        attempts=0,
    )
    session.add(notification)
    await session.commit()
    return notification


def issue_link_code(user: User) -> str:
    user.telegram_link_code = secrets.token_urlsafe(16)
    user.telegram_link_expires_at = datetime.now(timezone.utc) + LINK_CODE_LIFETIME
    return user.telegram_link_code


async def link_chat(session: AsyncSession, code: str, chat_id: int, username: str | None) -> User | None:
    user = await session.scalar(select(User).where(User.telegram_link_code == code))
    if not user or not user.telegram_link_expires_at or user.telegram_link_expires_at < datetime.now(timezone.utc):
        return None
    previous = await session.scalar(select(User).where(User.telegram_chat_id == chat_id, User.id != user.id))
    if previous:
        previous.telegram_chat_id = None
        previous.telegram_username = None
        await session.flush()
    user.telegram_chat_id = chat_id
    user.telegram_username = username
    user.telegram_linked_at = datetime.now(timezone.utc)
    user.telegram_link_code = None
    user.telegram_link_expires_at = None
    await get_settings(session, user)
    await import_legacy_bot_data(session, user, chat_id)
    await session.commit()
    return user


async def import_legacy_bot_data(session: AsyncSession, user: User, chat_id: int) -> None:
    calendar = await default_calendar(session, user, user_timezone(user))
    params = {"user_id": user.id, "chat_id": chat_id, "calendar_id": calendar.id, "timezone": user_timezone(user)}
    await session.execute(LEGACY_EVENTS, params)
    await session.execute(LEGACY_CONVERSATION, params)
    await session.execute(LEGACY_PLACEHOLDER_USER, {**params, "hashes": email_lookup(f"{chat_id}@local.invalid")})


async def snooze(session: AsyncSession, notification: Notification, minutes: int) -> Notification:
    now = datetime.now(timezone.utc)
    scheduled = now + timedelta(minutes=minutes)
    body = notification.text
    event = await session.get(Event, notification.event_id) if notification.event_id else None
    if event and event.start_at:
        user = await session.get(User, notification.user_id)
        lead = int((event.start_at - scheduled).total_seconds() // 60)
        body = reminder_text(event, lead, ZoneInfo(user_timezone(user)))
    copy = Notification(
        user_id=notification.user_id,
        event_id=notification.event_id,
        kind=notification.kind,
        dedupe_key=f"snooze:{notification.id}:{secrets.token_hex(6)}",
        text=body,
        scheduled_for=scheduled,
        expires_at=scheduled + timedelta(hours=2),
        status=NotificationStatus.PENDING,
        attempts=0,
    )
    session.add(copy)
    await session.commit()
    return copy
