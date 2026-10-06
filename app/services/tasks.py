"""Tasks on top of events: untimed tasks, recurring series, completion, statistics and rescheduling."""

import logging
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Event, User
from app.services.dates import describe_rrule, valid_rrule
from app.services.events import default_calendar, push_new_events_to_google
from app.services.integrations.service import user_timezone

logger = logging.getLogger(__name__)

SERIES_HORIZON = timedelta(days=90)
SERIES_REFILL = timedelta(days=30)
MAX_OCCURRENCES = 60
# One confirmation may not flood the calendar (40 daily series would otherwise be 2400 events)
MAX_EVENTS_PER_BATCH = 500
MAX_REMINDER_MINUTES = 10080
UNTIMED_TASK_MINUTES = 30
_last_extension: datetime | None = None


def local_tz(user: User) -> ZoneInfo:
    return ZoneInfo(user_timezone(user))


def event_bounds(day: date, start: time | None, end: time | None, tz: ZoneInfo) -> tuple[datetime, datetime, bool]:
    """Start, end and all_day for a task; a task without a time spans its whole day."""
    if start is None:
        midnight = datetime.combine(day, time.min, tz)
        return midnight, midnight + timedelta(days=1), True
    begins = datetime.combine(day, start, tz)
    ends = datetime.combine(day, end, tz) if end and end > start else begins + timedelta(hours=1)
    return begins, ends, False


def occurrences(rule: str, start: datetime, until: datetime, limit: int = MAX_OCCURRENCES, include_start: bool = True) -> list[datetime]:
    try:
        series = rrulestr(rule, dtstart=start)
    except (TypeError, ValueError):
        return [start] if include_start else []
    found = []
    for moment in series.xafter(start, count=limit + 1, inc=include_start):
        if moment > until or len(found) >= limit:
            break
        found.append(moment)
    return found


def _copy(template: Event, moment: datetime, duration: timedelta) -> Event:
    return Event(
        calendar_id=template.calendar_id,
        user_id=template.user_id,
        title=template.title,
        description=template.description,
        start_at=moment,
        end_at=moment + duration,
        timezone=template.timezone,
        location=template.location,
        priority=template.priority,
        all_day=template.all_day,
        reminder_minutes=template.reminder_minutes,
        recurrence_rule=template.recurrence_rule,
        series_id=template.series_id,
        source=template.source,
    )


def add_occurrences(session: AsyncSession, event: Event, now: datetime) -> list[Event]:
    """Turn an event with a recurrence rule into a series: the rest of the occurrences up to the horizon."""
    event.series_id = event.series_id or str(uuid4())
    try:
        tz = ZoneInfo(event.timezone)
    except (TypeError, ValueError, KeyError):
        tz = event.start_at.tzinfo
    anchor = event.start_at.astimezone(tz)
    moments = occurrences(event.recurrence_rule, anchor, now + SERIES_HORIZON, limit=MAX_OCCURRENCES - 1, include_start=False)
    extra = [_copy(event, moment, event.end_at - event.start_at) for moment in moments]
    session.add_all(extra)
    return extra


async def create_tasks(session: AsyncSession, user: User, items: list[dict], source: str = "ai") -> tuple[list[Event], list[int]]:
    """Create events from normalized items (see chat.normalize_item); returns the first event of each item and all ids."""
    tz = local_tz(user)
    calendar = await default_calendar(session, user, str(tz))
    now = datetime.now(tz)
    groups: list[list[Event]] = []
    for item in items:
        try:
            day = date.fromisoformat(item["date"])
            start_time = time.fromisoformat(item["time"]) if item.get("time") else None
            end_time = time.fromisoformat(item["end_time"]) if item.get("end_time") else None
        except (KeyError, TypeError, ValueError):
            continue
        title = str(item.get("title") or "").strip()[:300]
        if not title:
            continue
        begins, ends, all_day = event_bounds(day, start_time, end_time, tz)
        reminder = item.get("reminder_minutes")
        if not isinstance(reminder, int) or isinstance(reminder, bool) or not 0 <= reminder <= MAX_REMINDER_MINUTES:
            reminder = None
        rule = valid_rrule(item.get("rrule"))
        template = Event(
            calendar_id=calendar.id,
            user_id=user.id,
            title=title,
            description=str(item["description"]).strip() if item.get("description") else None,
            start_at=begins,
            end_at=ends,
            timezone=str(tz),
            location=str(item["location"]).strip()[:500] if item.get("location") else None,
            priority=item.get("priority") if item.get("priority") in ("low", "medium", "high", "urgent") else "medium",
            all_day=all_day,
            reminder_minutes=reminder,
            recurrence_rule=rule,
            series_id=str(uuid4()) if rule else None,
            source=source,
        )
        budget = MAX_EVENTS_PER_BATCH - sum(len(group) for group in groups)
        if budget <= 0:
            break
        moments = occurrences(rule, begins, now + SERIES_HORIZON, limit=min(MAX_OCCURRENCES, budget)) if rule else [begins]
        group = [_copy(template, moment, ends - begins) for moment in moments or [begins]]
        session.add_all(group)
        groups.append(group)
    if not groups:
        return [], []
    await session.commit()
    created = [event for group in groups for event in group]
    for event in created:
        await session.refresh(event)
    await push_new_events_to_google(session, user.id, created)
    return [group[0] for group in groups], [event.id for event in created]


async def extend_series(session: AsyncSession, now: datetime, force: bool = False) -> int:
    """Keep recurring series filled SERIES_HORIZON ahead; runs at most every half hour."""
    global _last_extension
    if not force and _last_extension and now - _last_extension < timedelta(minutes=30):
        return 0
    _last_extension = now
    rows = await session.execute(
        select(Event.series_id, func.max(Event.start_at))
        .where(Event.series_id.is_not(None), Event.recurrence_rule.is_not(None))
        .group_by(Event.series_id)
        .having(func.max(Event.start_at) < now + SERIES_REFILL)
        .limit(200)
    )
    added = 0
    for series_id, latest_start in rows.all():
        latest = await session.scalar(select(Event).where(Event.series_id == series_id, Event.start_at == latest_start).limit(1))
        if not latest or not valid_rrule(latest.recurrence_rule):
            continue
        tz = ZoneInfo(latest.timezone or "UTC")
        anchor = latest.start_at.astimezone(tz)
        moments = occurrences(latest.recurrence_rule, anchor, now + SERIES_HORIZON, include_start=False)
        session.add_all(_copy(latest, moment, latest.end_at - latest.start_at) for moment in moments)
        added += len(moments)
    if added:
        await session.commit()
        logger.info("Extended recurring series with %d events", added)
    return added


async def delete_series(session: AsyncSession, user: User, event: Event) -> int:
    """Delete this and later occurrences; earlier ones stay as history but stop the series from refilling."""
    if not event.series_id:
        await session.delete(event)
        await session.commit()
        return 1
    later = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.series_id == event.series_id, Event.start_at >= event.start_at)))
    for item in later:
        await session.delete(item)
    await session.execute(update(Event).where(Event.user_id == user.id, Event.series_id == event.series_id).values(recurrence_rule=None))
    await session.commit()
    return len(later)


async def set_completed(session: AsyncSession, user: User, event_id: int, completed: bool) -> Event | None:
    event = await session.get(Event, event_id)
    if not event or event.user_id != user.id:
        return None
    event.completed_at = datetime.now(timezone.utc) if completed else None
    await session.commit()
    await session.refresh(event)
    return event


async def events_between(session: AsyncSession, user: User, start: datetime, end: datetime, limit: int = 500) -> list[Event]:
    return list(
        await session.scalars(
            select(Event)
            .where(Event.user_id == user.id, Event.start_at < end, Event.end_at > start)
            .order_by(Event.all_day.desc(), Event.start_at)
            .limit(limit)
        )
    )


def is_done(event: Event, now: datetime) -> bool:
    """A task is done when marked so; a timed event (a meeting, an appointment) also once its time is over."""
    return event.completed_at is not None or (not event.all_day and event.end_at <= now)


def is_overdue(event: Event, today: date, tz: ZoneInfo) -> bool:
    """Only a one-off task without a time can be overdue: past events simply happened,
    and a missed day of a recurring task does not pile up."""
    return event.all_day and event.completed_at is None and event.series_id is None and event.start_at.astimezone(tz).date() < today


def _percent(done: int, total: int) -> int:
    return round(done * 100 / total) if total else 0


async def daily_stats(session: AsyncSession, user: User, days: int = 7) -> dict:
    """Completed vs planned tasks per day for the last `days` days, ending today."""
    tz = local_tz(user)
    now = datetime.now(tz)
    today = now.date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, tz)
    events = await events_between(session, user, start, datetime.combine(today + timedelta(days=1), time.min, tz), limit=5000)
    per_day = {first + timedelta(days=offset): [0, 0] for offset in range(days)}
    for event in events:
        day = event.start_at.astimezone(tz).date()
        if day in per_day:
            per_day[day][0] += 1
            per_day[day][1] += is_done(event, now)
    series = [{"date": day.isoformat(), "total": total, "done": done, "percent": _percent(done, total)} for day, (total, done) in per_day.items()]
    streak = 0
    for entry in reversed(series):
        if entry["date"] == today.isoformat() and entry["done"] == 0:
            continue  # today may still be in progress
        if entry["total"] and entry["done"] == entry["total"]:
            streak += 1
        else:
            break
    total = sum(entry["total"] for entry in series)
    done = sum(entry["done"] for entry in series)
    return {"today": series[-1], "days": series, "total": total, "done": done, "percent": _percent(done, total), "streak": streak}


async def move_events(session: AsyncSession, user: User, event_ids: list[int], target: date) -> int:
    """Move tasks to another day, keeping their time of day and duration."""
    tz = local_tz(user)
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(event_ids))))
    for event in events:
        local_start = event.start_at.astimezone(tz)
        duration = event.end_at - event.start_at
        event.start_at = datetime.combine(target, local_start.time(), tz)
        event.end_at = event.start_at + duration
        event.sync_status = "pending" if event.external_id else event.sync_status
    await session.commit()
    return len(events)


def task_view(event: Event, tz: ZoneInfo) -> dict:
    start = event.start_at.astimezone(tz)
    return {
        "id": event.id,
        "title": event.title,
        "start": start.isoformat(),
        "end": event.end_at.astimezone(tz).isoformat(),
        "date": start.date().isoformat(),
        "time": None if event.all_day else f"{start:%H:%M}",
        "all_day": event.all_day,
        "location": event.location,
        "description": event.description,
        "priority": event.priority,
        "reminder_minutes": event.reminder_minutes,
        "completed": event.completed_at is not None,
        "recurrence": describe_rrule(event.recurrence_rule),
        "series_id": event.series_id,
    }
