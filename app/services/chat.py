import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import ConversationMessage, Event, User
from app.services.events import default_calendar, google_provider, push_new_events_to_google, remember_google_token
from app.services.integrations.service import user_timezone
from app.services.ru import MONTHS, day_label

logger = logging.getLogger(__name__)

CONTEXT_MESSAGES = 12
CONTEXT_DAYS = 30
RECURRENCE_WINDOW = timedelta(days=60)
MAX_OCCURRENCES = 12
MAX_REMINDER_MINUTES = 10080
UNDO_WINDOW = timedelta(days=7)

QUESTION_PREFIXES = (
    "что ", "что?", "какие ", "какая ", "какой ", "покажи", "есть ли ", "когда ", "во сколько ", "где у меня",
    "план ", "планы", "план?", "расписание", "мои ", "мой ", "сколько у меня",
)
STOP_WORDS = {
    "что", "есть", "ли", "у", "меня", "мне", "мои", "мой", "план", "планы", "планов", "планах", "какие", "какая",
    "какой", "покажи", "когда", "сколько", "расписание", "сегодня", "завтра", "послезавтра", "через", "неделю",
    "неделе", "недели", "месяц", "месяца", "день", "дня", "дней", "будет", "было", "нужно", "должен", "должна",
    "утром", "днем", "днём", "вечером", "ночью", "события", "событие", "событий", "дела", "делам",
    "выходные", "выходных", "выходным", "этой", "следующей", "следующую",
}
WEEKDAY_PATTERNS = (
    (0, r"понедельн\w*|пн"),
    (1, r"вторник\w*|вт"),
    (2, r"сред[ауеы]|ср"),
    (3, r"четверг\w*|чт"),
    (4, r"пятниц\w*|пт"),
    (5, r"суббот\w*|сб"),
    (6, r"воскресень\w*|вс"),
)


class AssistantUnavailable(Exception):
    pass


def event_view(event: Event, tz: ZoneInfo, repeats: int = 0) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "start": event.start_at.astimezone(tz).isoformat(),
        "end": event.end_at.astimezone(tz).isoformat(),
        "all_day": event.all_day,
        "location": event.location,
        "description": event.description,
        "priority": event.priority,
        "reminder_minutes": event.reminder_minutes,
        "repeats": repeats,
        "url": f"{settings.public_app_url}/app/events/{event.id}" if settings.public_app_url else None,
    }


def group_by_day(events: list[Event], tz: ZoneInfo, today: date) -> list[dict]:
    days: dict[date, list[dict]] = {}
    for event in sorted(events, key=lambda item: item.start_at):
        days.setdefault(event.start_at.astimezone(tz).date(), []).append(event_view(event, tz))
    return [{"date": day.isoformat(), "label": day_label(day, today), "events": items} for day, items in sorted(days.items())]


async def remember(session: AsyncSession, user_id: int, role: str, content: str) -> None:
    session.add(ConversationMessage(user_id=user_id, role=role, content=content[:4000], created_at=datetime.now(timezone.utc)))


async def recent_context(session: AsyncSession, user_id: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=CONTEXT_DAYS)
    await session.execute(delete(ConversationMessage).where(ConversationMessage.user_id == user_id, ConversationMessage.created_at < since))
    rows = await session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user_id)
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(CONTEXT_MESSAGES)
    )
    return "\n".join(f"{row.role}: {row.content[:1200]}" for row in reversed(list(rows)))


def is_question(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    return lowered.startswith(QUESTION_PREFIXES) or bool(re.search(r"\bчто у меня\b|\bкогда у меня\b|\bчем я занят", lowered))


def requested_weekday(text: str) -> int | None:
    lowered = text.lower()
    for weekday, pattern in WEEKDAY_PATTERNS:
        if re.search(rf"(?<![а-яё])(?:{pattern})(?![а-яё])", lowered):
            return weekday
    return None


def search_keywords(text: str) -> list[str]:
    words = re.findall(r"[а-яёa-z0-9]+", text.lower())
    return [word[:5] for word in words if len(word) >= 4 and word not in STOP_WORDS][:6]


def local_filters(text: str, tz: ZoneInfo) -> dict | None:
    lowered = text.lower()
    today = datetime.now(tz).date()
    date_from = date_to = None
    if "послезавтра" in lowered:
        date_from = date_to = today + timedelta(days=2)
    elif "завтра" in lowered:
        date_from = date_to = today + timedelta(days=1)
    elif "сегодня" in lowered:
        date_from = date_to = today
    elif re.search(r"на (этой|эту) недел|на неделе|за неделю", lowered):
        date_from, date_to = today, today + timedelta(days=6 - today.weekday())
    elif "следующей недел" in lowered or "следующую неделю" in lowered:
        start = today + timedelta(days=7 - today.weekday())
        date_from, date_to = start, start + timedelta(days=6)
    elif "выходн" in lowered:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        date_from, date_to = saturday, saturday + timedelta(days=1)
    elif match := re.search(r"через\s+(\d+)\s+(дн\w*|недел\w*|месяц\w*)", lowered):
        amount, unit = int(match.group(1)), match.group(2)
        target = today + (timedelta(days=amount) if unit.startswith("дн") else timedelta(weeks=amount) if unit.startswith("недел") else relativedelta(months=amount))
        date_from = date_to = target
    elif (weekday := requested_weekday(text)) is not None:
        date_from = date_to = today + timedelta(days=(weekday - today.weekday()) % 7)
    time_from = time_to = None
    if "утр" in lowered:
        time_from, time_to = time(5), time(12)
    elif "днем" in lowered or "днём" in lowered:
        time_from, time_to = time(12), time(18)
    elif "вечер" in lowered:
        time_from, time_to = time(18), time(23, 59, 59)
    keywords = search_keywords(text)
    if not (date_from or time_from or keywords):
        return None
    return {"date_from": date_from, "date_to": date_to, "time_from": time_from, "time_to": time_to, "keywords": keywords}


def coerce_filters(raw: dict) -> dict:
    def as_date(value):
        try:
            return date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def as_time(value):
        try:
            return time.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    keywords = [str(word)[:40] for word in raw.get("keywords") or [] if isinstance(word, str) and word.strip()][:6]
    return {
        "date_from": as_date(raw.get("date_from")),
        "date_to": as_date(raw.get("date_to")),
        "time_from": as_time(raw.get("time_from")),
        "time_to": as_time(raw.get("time_to")),
        "keywords": keywords,
    }


def search_title(filters: dict, today: date) -> str:
    date_from, date_to = filters["date_from"], filters["date_to"]
    if date_from and date_from == date_to:
        label = day_label(date_from, today)
        return label.split(" · ")[0] if " · " in label else label
    if date_from and date_to:
        return f"{date_from.day} {MONTHS[date_from.month - 1]} — {date_to.day} {MONTHS[date_to.month - 1]}"
    return "Найденные события"


async def search(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    filters = local_filters(text, tz)
    if filters is None and settings.gigachat_credentials:
        from services.gigachat import GigaChatClient

        try:
            filters = coerce_filters(await GigaChatClient().extract_search_filters(text, str(tz)))
        except Exception:
            logger.exception("GigaChat search filters failed")
    filters = filters or {"date_from": None, "date_to": None, "time_from": None, "time_to": None, "keywords": []}
    now = datetime.now(tz)
    since = datetime.combine(filters["date_from"], time.min, tz) if filters["date_from"] else now - timedelta(hours=12)
    until = datetime.combine(filters["date_to"] + timedelta(days=1), time.min, tz) if filters["date_to"] else since + timedelta(days=60)
    conditions = [Event.user_id == user.id, Event.start_at.is_not(None), Event.start_at < until, Event.end_at > since]
    if filters["keywords"] and not filters["date_from"]:
        conditions.append(or_(*[column.ilike(f"%{word}%") for word in filters["keywords"] for column in (Event.title, Event.description, Event.location)]))
    events = list(await session.scalars(select(Event).where(*conditions).order_by(Event.start_at).limit(100)))
    if filters["time_from"] or filters["time_to"]:
        events = [
            event
            for event in events
            if (not filters["time_from"] or event.start_at.astimezone(tz).time() >= filters["time_from"])
            and (not filters["time_to"] or event.start_at.astimezone(tz).time() < filters["time_to"])
        ]
    single_day = filters["date_from"] if filters["date_from"] and filters["date_from"] == filters["date_to"] else None
    return {
        "kind": "agenda",
        "title": search_title(filters, now.date()),
        "date": single_day.isoformat() if single_day else None,
        "days": group_by_day(events, tz, now.date()),
    }


async def agenda(session: AsyncSession, user: User, scope: str) -> dict:
    tz = ZoneInfo(user_timezone(user))
    today = datetime.now(tz).date()
    first = today + timedelta(days=1) if scope == "tomorrow" else today
    length = 7 if scope == "week" else 1
    start = datetime.combine(first, time.min, tz)
    end = start + timedelta(days=length)
    events = list(
        await session.scalars(
            select(Event)
            .where(Event.user_id == user.id, Event.start_at.is_not(None), Event.start_at < end, Event.end_at > start)
            .order_by(Event.start_at)
            .limit(200)
        )
    )
    titles = {"today": "Сегодня", "tomorrow": "Завтра", "week": "Ближайшие 7 дней"}
    return {"kind": "agenda", "scope": scope, "title": titles.get(scope, "План"), "date": first.isoformat(), "days": group_by_day(events, tz, today)}


def grounded(item: dict, text: str) -> bool:
    request = {word[:4] for word in re.findall(r"[а-яёa-z]+", text.lower()) if len(word) >= 4}
    event_text = " ".join(str(item.get(field) or "") for field in ("title", "description", "location")).lower()
    return any(word[:4] in request for word in re.findall(r"[а-яёa-z]+", event_text) if len(word) >= 4)


def parse_moment(value, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def occurrences(start: datetime, rule: str | None) -> list[datetime]:
    if not isinstance(rule, str) or not rule.strip():
        return [start]
    try:
        series = rrulestr(rule.strip().removeprefix("RRULE:"), dtstart=start)
    except (TypeError, ValueError):
        return [start]
    found = []
    for moment in series:
        if moment > start + RECURRENCE_WINDOW or len(found) >= MAX_OCCURRENCES:
            break
        found.append(moment)
    return found or [start]


async def create_events(session: AsyncSession, user: User, items: list[dict], tz: ZoneInfo) -> tuple[list[dict], list[int]]:
    tz_name = str(tz)
    calendar = await default_calendar(session, user, tz_name)
    series: list[list[Event]] = []
    for item in items:
        title = str(item.get("title") or "").strip()[:300]
        start = parse_moment(item.get("starts_at"), tz)
        if not title or not start:
            continue
        end = parse_moment(item.get("ends_at"), tz)
        duration = end - start if end and end > start else timedelta(hours=1)
        reminder = item.get("reminder_minutes")
        if not isinstance(reminder, int) or isinstance(reminder, bool) or not 0 <= reminder <= MAX_REMINDER_MINUTES:
            reminder = None
        location = str(item["location"]).strip()[:500] if item.get("location") else None
        description = str(item["description"]).strip() if item.get("description") else None
        created = []
        for moment in occurrences(start, item.get("recurrence_rule")):
            event = Event(
                calendar_id=calendar.id,
                user_id=user.id,
                title=title,
                description=description,
                start_at=moment,
                end_at=moment + duration,
                timezone=tz_name,
                location=location,
                reminder_minutes=reminder,
                source="ai",
            )
            session.add(event)
            created.append(event)
        series.append(created)
    if not series:
        return [], []
    await session.commit()
    all_events = [event for group in series for event in group]
    for event in all_events:
        await session.refresh(event)
    await push_new_events_to_google(session, user.id, all_events)
    return [event_view(group[0], tz, repeats=len(group) - 1) for group in series], [event.id for event in all_events]


def summarize(reply: dict) -> str:
    if reply["kind"] == "created":
        return "Добавила в календарь: " + "; ".join(f"{event['title']} ({event['start']})" for event in reply["events"])
    if reply["kind"] == "agenda":
        count = sum(len(day["events"]) for day in reply["days"])
        return f"Показала события ({reply['title']}): {count}"
    return reply.get("text") or "Не нашла событий в сообщении"


async def handle_message(session: AsyncSession, user: User, text: str) -> dict:
    tz = ZoneInfo(user_timezone(user))
    text = text.strip()[:50000]
    history = await recent_context(session, user.id)
    if is_question(text):
        reply = await search(session, user, text, tz)
    else:
        if not settings.gigachat_credentials:
            raise AssistantUnavailable
        from services.gigachat import GigaChatClient

        try:
            result = await GigaChatClient().process_message(text, str(tz), history)
        except Exception as error:
            logger.exception("GigaChat request failed")
            raise AssistantUnavailable from error
        items = [item for item in result.get("events", []) if isinstance(item, dict)]
        if history:
            items = [item for item in items if grounded(item, text)]
        events, ids = await create_events(session, user, items, tz)
        answer = result.get("answer")
        if not events and not answer:
            try:
                answer = await GigaChatClient().chat_reply(text, str(tz), history, user.name)
            except Exception:
                logger.exception("GigaChat chat reply failed")
        if events:
            reply = {"kind": "created", "events": events, "event_ids": ids, "answer": answer}
        elif answer:
            reply = {"kind": "answer", "text": answer}
        else:
            reply = {"kind": "nothing"}
    await remember(session, user.id, "user", text)
    await remember(session, user.id, "assistant", summarize(reply))
    await session.commit()
    return reply


async def undo(session: AsyncSession, user: User, event_ids: list[int]) -> int:
    since = datetime.now(timezone.utc) - UNDO_WINDOW
    events = list(await session.scalars(select(Event).where(Event.id.in_(event_ids), Event.user_id == user.id, Event.created_at >= since)))
    if not events:
        return 0
    synced = [event for event in events if event.external_id and event.source == "google"]
    if synced:
        integration, provider = await google_provider(session, user.id)
        if provider:
            for event in synced:
                try:
                    await provider.delete_event("primary", event.external_id)
                except Exception:
                    logger.warning("Could not delete Google event %s", event.external_id)
            remember_google_token(integration, provider)
    for event in events:
        await session.delete(event)
    await session.commit()
    return len(events)
