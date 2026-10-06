import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import snowballstemmer
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import AssistantDraft, ConversationMessage, Event, User
from app.services import dates, tasks
from app.services.events import google_provider, remember_google_token
from app.services.ru import MONTHS, day_label

logger = logging.getLogger(__name__)

CONTEXT_MESSAGES = 12
CONTEXT_DAYS = 30
UNDO_WINDOW = timedelta(days=7)
DRAFT_LIFETIME = timedelta(days=2)
# A message counts as the new value of a field only shortly after the user tapped "edit"
EDIT_TIMEOUT = timedelta(minutes=10)
MAX_DRAFT_ITEMS = 40
LOCAL_TEXT_LIMIT = 300
SEARCH_PAST = timedelta(days=30)
SEARCH_AHEAD = timedelta(days=365)
SEARCH_SCAN_LIMIT = 2000
NOT_FOUND_TEXT = (
    "Задача не найдена. Попробуйте уточнить запрос: укажите слово из названия, дату или период — "
    "например, «когда встреча с Анной?» или «что у меня в пятницу?»"
)
EDIT_PROMPTS = {
    "title": "Напишите новое название",
    "date": "Напишите новую дату: «завтра», «в пятницу», «через 2 дня», «15 октября»",
    "time": "Напишите новое время: «18:00», «с 10 до 12» — или «без времени»",
}
CANCEL_WORDS = {"отмена", "отменить", "стоп", "/cancel"}
NO_TIME_WORDS = re.compile(r"^\s*(без\s+времени|весь\s+день|убрать(\s+время)?|нет|не\s+важно|любое)\s*\.?\s*$", re.I)

QUESTION_PREFIXES = (
    "что ", "что?", "какие ", "какая ", "какой ", "покажи", "есть ли ", "когда ", "во сколько ", "где у меня",
    "план ", "планы", "план?", "расписание", "мои ", "мой ", "сколько у меня", "найди ", "найти ", "где ",
    "напомни, что", "что запланировано", "свободен ли", "занят ли",
)
STOP_WORDS = {
    "что", "есть", "ли", "у", "меня", "мне", "мои", "мой", "моя", "моё", "мое", "план", "планы", "планов", "планах",
    "какие", "какая", "какой", "покажи", "показать", "когда", "сколько", "расписание", "сегодня", "завтра", "послезавтра",
    "через", "неделю", "неделе", "недели", "месяц", "месяца", "день", "дня", "дней", "будет", "было", "была", "был",
    "нужно", "надо", "должен", "должна", "утром", "днем", "днём", "вечером", "ночью", "события", "событие", "событий",
    "дела", "делам", "дело", "выходные", "выходных", "выходным", "этой", "этот", "эту", "следующей", "следующую",
    "следующий", "задача", "задачу", "задачи", "задач", "задаче", "найди", "найти", "где", "во", "запланировано",
    "запланирована", "запланирован", "назначено", "назначена", "назначен", "время", "часов", "пожалуйста", "напомни",
    "все", "всё", "всех", "list", "about", "расскажи", "расскажите", "подскажи", "скажи", "покажите", "посмотри",
    "про", "обо", "свободен", "свободна", "занят", "занята", "планирую", "запланировал", "запланировала", "моих", "моя",
}
_stemmer = snowballstemmer.stemmer("russian")


class AssistantUnavailable(Exception):
    pass


class DraftNotFound(Exception):
    pass


# ---------- conversation memory ----------


HISTORY_LIMIT = 60
MAX_STORED_REPLY = 60_000


def storable(reply: dict | None) -> dict | None:
    """The reply as kept in the history; a huge agenda is kept as its summary."""
    if reply is None:
        return None
    if len(json.dumps(reply, ensure_ascii=False, default=str)) > MAX_STORED_REPLY:
        return {"kind": "answer", "text": summarize(reply)}
    return reply


async def remember(session: AsyncSession, user_id: int, role: str, content: str, reply: dict | None = None) -> None:
    session.add(
        ConversationMessage(
            user_id=user_id,
            role=role,
            content=content[:4000],
            reply=storable(reply),
            draft_id=reply.get("draft_id") if reply and reply.get("kind") == "proposal" else None,
            created_at=datetime.now(timezone.utc),
        )
    )


async def update_draft_messages(session: AsyncSession, user: User, draft_id: int, reply: dict) -> None:
    """Show the draft's latest state (edited, added or cancelled) wherever the history shows it."""
    messages = list(await session.scalars(select(ConversationMessage).where(ConversationMessage.user_id == user.id, ConversationMessage.draft_id == draft_id)))
    for message in messages:
        message.reply = storable(reply)
        message.content = summarize(reply)[:4000]
    if not messages and reply.get("kind") == "created":
        await remember(session, user.id, "assistant", summarize(reply), reply)


async def history(session: AsyncSession, user: User, limit: int = HISTORY_LIMIT) -> list[dict]:
    rows = list(
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user.id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        )
    )
    return [
        {"id": row.id, "role": row.role, "text": row.content, "reply": row.reply, "created_at": row.created_at.isoformat()}
        for row in reversed(rows)
    ]


async def calendar_context(session: AsyncSession, user: User, tz: ZoneInfo) -> str:
    """The user's real plan for the assistant: today and the next 7 days, plus unfinished tasks."""
    now = datetime.now(tz)
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today - timedelta(days=7), time.min, tz), datetime.combine(today + timedelta(days=8), time.min, tz))
    lines = []
    for event in events:
        start = event.start_at.astimezone(tz)
        overdue = tasks.is_overdue(event, today, tz)
        if start.date() < today and not overdue:
            continue
        when = "без времени" if event.all_day else f"{start:%H:%M}"
        mark = " (выполнено)" if event.completed_at else " (не выполнено, просрочено)" if overdue else ""
        lines.append(f"{day_label(start.date(), today)}, {when}: {event.title}{mark}")
        if len(lines) >= 60:
            break
    return "\n".join(lines) or "(в календаре на ближайшую неделю ничего нет)"


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


# ---------- agenda and search ----------


def event_view(event: Event, tz: ZoneInfo, repeats: int = 0) -> dict:
    view = tasks.task_view(event, tz)
    view["repeats"] = repeats
    view["url"] = f"{settings.public_app_url}/app/events/{event.id}" if settings.public_app_url else None
    return view


def group_by_day(events: list[Event], tz: ZoneInfo, today: date) -> list[dict]:
    days: dict[date, list[dict]] = {}
    for event in sorted(events, key=lambda item: (item.start_at.astimezone(tz).date(), not item.all_day, item.start_at)):
        days.setdefault(event.start_at.astimezone(tz).date(), []).append(event_view(event, tz))
    return [{"date": day.isoformat(), "label": day_label(day, today), "events": items} for day, items in sorted(days.items())]


def is_question(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    return lowered.startswith(QUESTION_PREFIXES) or bool(
        re.search(
            r"\bчто у меня\b|\bкогда у меня\b|\bчем я занят|\b(мои|какие у меня)\s+(задач|план|дел|событ|встреч)"
            r"|\b(расскажи|подскажи|скажи|покажи)\b.*\b(план|расписан|задач|дел|событ|встреч)",
            lowered,
        )
    )


def search_keywords(text: str) -> list[str]:
    stems = []
    for word in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")):
        if len(word) < 3 or word in STOP_WORDS or word.isdigit():
            continue
        stem = _stemmer.stemWord(word)
        if len(stem) >= 3 and stem not in stems:
            stems.append(stem)
    return stems[:5]


def local_filters(text: str, tz: ZoneInfo) -> dict | None:
    lowered = text.lower()
    now = datetime.now(tz)
    today = now.date()
    date_from = date_to = None
    if re.search(r"на (этой|эту) недел|на неделе|за неделю", lowered):
        date_from, date_to = today, today + timedelta(days=6 - today.weekday())
    elif "следующей недел" in lowered or "следующую неделю" in lowered:
        start = today + timedelta(days=7 - today.weekday())
        date_from, date_to = start, start + timedelta(days=6)
    elif "выходн" in lowered:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        date_from, date_to = saturday, saturday + timedelta(days=1)
    else:
        parsed = dates.parse(text, now)
        date_from = date_to = parsed.date
    time_from = time_to = None
    if "утр" in lowered:
        time_from, time_to = time(5), time(12)
    elif "днем" in lowered or "днём" in lowered:
        time_from, time_to = time(12), time(18)
    elif "вечер" in lowered:
        time_from, time_to = time(18), time(23, 59, 59)
    keywords = search_keywords(dates.strip_spans(text, dates.parse(text, now).spans))
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

    words = " ".join(str(word) for word in raw.get("keywords") or [] if isinstance(word, str))
    return {
        "date_from": as_date(raw.get("date_from")),
        "date_to": as_date(raw.get("date_to")),
        "time_from": as_time(raw.get("time_from")),
        "time_to": as_time(raw.get("time_to")),
        "keywords": search_keywords(words),
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
    if not filters or not (filters["date_from"] or filters["time_from"] or filters["keywords"]):
        # Nothing to search by: ask to refine instead of dumping every task
        return {"kind": "not_found", "text": NOT_FOUND_TEXT}
    now = datetime.now(tz)
    if filters["date_from"]:
        since = datetime.combine(filters["date_from"], time.min, tz)
        until = datetime.combine((filters["date_to"] or filters["date_from"]) + timedelta(days=1), time.min, tz)
    elif filters["keywords"]:
        since, until = now - SEARCH_PAST, now + SEARCH_AHEAD
    else:
        since, until = now - timedelta(hours=12), now + timedelta(days=7)
    conditions = [Event.user_id == user.id, Event.start_at < until, Event.end_at > since]
    events = list(await session.scalars(select(Event).where(*conditions).order_by(Event.start_at).limit(SEARCH_SCAN_LIMIT)))
    if filters["keywords"]:
        # Titles are encrypted at rest, so keywords are matched after decryption
        def searchable(event: Event) -> str:
            return " ".join(part for part in (event.title, event.description, event.location) if part).lower().replace("ё", "е")

        events = [event for event in events if all(stem in searchable(event) for stem in filters["keywords"])]
    events = events[:100]
    if filters["time_from"] or filters["time_to"]:
        events = [
            event
            for event in events
            if not event.all_day
            and (not filters["time_from"] or event.start_at.astimezone(tz).time() >= filters["time_from"])
            and (not filters["time_to"] or event.start_at.astimezone(tz).time() < filters["time_to"])
        ]
    if not events and filters["keywords"]:
        return {"kind": "not_found", "text": NOT_FOUND_TEXT}
    single_day = filters["date_from"] if filters["date_from"] and filters["date_from"] == filters["date_to"] else None
    return {
        "kind": "agenda",
        "title": search_title(filters, now.date()) if filters["date_from"] else "Найденные события",
        "date": single_day.isoformat() if single_day else None,
        "days": group_by_day(events, tz, now.date()),
    }


async def agenda(session: AsyncSession, user: User, scope: str) -> dict:
    tz = tasks.local_tz(user)
    today = datetime.now(tz).date()
    first = today + timedelta(days=1) if scope == "tomorrow" else today
    length = 7 if scope == "week" else 1
    start = datetime.combine(first, time.min, tz)
    events = await tasks.events_between(session, user, start, start + timedelta(days=length), limit=200)
    titles = {"today": "Сегодня", "tomorrow": "Завтра", "week": "Ближайшие 7 дней"}
    return {"kind": "agenda", "scope": scope, "title": titles.get(scope, "План"), "date": first.isoformat(), "days": group_by_day(events, tz, today)}


# ---------- turning model output into draft items ----------


def grounded(item: dict, text: str) -> bool:
    request = {word[:4] for word in re.findall(r"[а-яёa-z]+", text.lower()) if len(word) >= 4}
    event_text = " ".join(str(item.get(field) or "") for field in ("title", "description", "location")).lower()
    return any(word[:4] in request for word in re.findall(r"[а-яёa-z]+", event_text) if len(word) >= 4)


def _llm_date(raw: dict) -> date | None:
    for key in ("date", "starts_at"):
        value = raw.get(key)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def _llm_time(raw: dict, key: str) -> time | None:
    value = raw.get(key)
    if key == "start_time" and not value and isinstance(raw.get("starts_at"), str) and "T" in raw["starts_at"]:
        value = raw["starts_at"].split("T", 1)[1][:5]
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value.strip()[:5])
    except ValueError:
        return None


def mentions_time(text: str) -> bool:
    return bool(re.search(r"\d{1,2}[:.]\d{2}|\b(?:в|к|с)\s+\d{1,2}\b|полдень|полночь|половин|через\s+\S*\s*(?:минут|час|полчаса)", text.lower()))


def build_item(title: str, parsed: dates.Parsed, now: datetime, llm: dict | None = None, allow_llm_time: bool = False) -> dict | None:
    """A draft item with a concrete date. A task without a stated time stays untimed instead of getting 09:00."""
    llm = llm or {}
    title = " ".join(str(title or "").split()).strip()[:300]
    if not title:
        return None
    today = now.date()
    llm_day = _llm_date(llm)
    day = parsed.date or (llm_day if llm_day and llm_day >= today else None)
    start = parsed.time if parsed.time is not None else (_llm_time(llm, "start_time") if allow_llm_time else None)
    end = parsed.end_time if parsed.time is not None and parsed.end_time else (_llm_time(llm, "end_time") if allow_llm_time else None)
    duration = llm.get("duration_minutes")
    if start and not end and isinstance(duration, int) and not isinstance(duration, bool) and 0 < duration <= 24 * 60:
        end = (datetime.combine(today, start) + timedelta(minutes=duration)).time()
        end = end if end > start else None
    rule = parsed.rrule or (dates.valid_rrule(llm.get("recurrence_rule")) if llm.get("recurrence_phrase") else None)
    if day is None:
        day = today + timedelta(days=1) if start and start <= now.time() and not rule else today
    if rule:
        first = dates.first_occurrence(rule, datetime.combine(day, start or time.min, now.tzinfo), now)
        if first is None:
            rule = None
        else:
            day = first.date()
    reminder = llm.get("reminder_minutes")
    return {
        "title": title,
        "date": day.isoformat(),
        "time": start.strftime("%H:%M") if start else None,
        "end_time": end.strftime("%H:%M") if start and end and end > start else None,
        "rrule": rule,
        "location": str(llm["location"]).strip()[:500] if llm.get("location") else None,
        "description": str(llm["description"]).strip()[:2000] if llm.get("description") else None,
        "reminder_minutes": reminder if isinstance(reminder, int) and not isinstance(reminder, bool) and 0 <= reminder <= tasks.MAX_REMINDER_MINUTES else None,
    }


def normalize_item(raw: dict, text: str, now: datetime, single: bool) -> dict | None:
    phrases = [raw.get(key) for key in ("date_phrase", "time_phrase", "recurrence_phrase")]
    phrase = " ".join(value for value in phrases if isinstance(value, str) and value.strip())
    parsed = dates.parse(phrase, now) if phrase else dates.Parsed()
    if single:
        # With one task the whole message is about it, so phrases the model missed still count
        whole = dates.parse(text, now)
        parsed.date = parsed.date or whole.date
        parsed.rrule = parsed.rrule or whole.rrule
        if parsed.time is None:
            parsed.time, parsed.end_time = whole.time, whole.end_time
    return build_item(str(raw.get("title") or ""), parsed, now, raw, allow_llm_time=bool(raw.get("time_phrase")) or mentions_time(text))


FILLER = re.compile(
    r"^(?:(?:пожалуйста|напомни(?:те)?|напоминание|мне|надо|нужно|запиши|запланируй|добавь|поставь|создай(?:\s+задачу)?|задача|задачу|что|о том,?\s*что)\s*[:,]?\s+)+",
    re.I,
)


def clean_title(value: str) -> str:
    value = " ".join(str(value or "").split()).strip(" ,.;:—-")
    value = FILLER.sub("", value).strip(" ,.;:—-")
    value = re.sub(r"\s+(?:в|во|на|к|с|до)$", "", value, flags=re.I).strip(" ,.;:—-")
    return (value[:1].upper() + value[1:])[:300] if value else ""


def local_items(text: str, now: datetime) -> list[dict]:
    """Parse a short single task without the model: "купить хлеб завтра в 18:00"."""
    if len(text) > LOCAL_TEXT_LIMIT or "\n" in text.strip():
        return []
    parsed = dates.parse(text, now)
    if parsed.empty:
        return []
    item = build_item(clean_title(dates.strip_spans(text, parsed.spans)), parsed, now)
    return [item] if item else []


# ---------- drafts: the confirmation stage ----------


def item_view(item: dict, index: int, tz: ZoneInfo) -> dict:
    day = date.fromisoformat(item["date"])
    start, end, all_day = tasks.event_bounds(
        day,
        time.fromisoformat(item["time"]) if item.get("time") else None,
        time.fromisoformat(item["end_time"]) if item.get("end_time") else None,
        tz,
    )
    return {
        **item,
        "index": index,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": all_day,
        "recurrence": dates.describe_rrule(item.get("rrule")),
    }


def proposal(draft: AssistantDraft, tz: ZoneInfo, answer: str | None = None, note: str | None = None) -> dict:
    return {
        "kind": "proposal",
        "draft_id": draft.id,
        "events": [item_view(item, index, tz) for index, item in enumerate(draft.items)],
        "answer": answer,
        "note": note,
        "awaiting": draft.awaiting,
    }


async def create_draft(session: AsyncSession, user: User, items: list[dict]) -> AssistantDraft:
    await session.execute(
        delete(AssistantDraft).where(AssistantDraft.user_id == user.id, AssistantDraft.created_at < datetime.now(timezone.utc) - DRAFT_LIFETIME)
    )
    # Only the newest draft can wait for an edit
    drafts = await session.scalars(select(AssistantDraft).where(AssistantDraft.user_id == user.id, AssistantDraft.awaiting.is_not(None)))
    for old in drafts:
        old.awaiting = None
    draft = AssistantDraft(user_id=user.id, items=items[:MAX_DRAFT_ITEMS], awaiting=None)
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return draft


async def get_draft(session: AsyncSession, user: User, draft_id: int) -> AssistantDraft:
    draft = await session.get(AssistantDraft, draft_id)
    if not draft or draft.user_id != user.id:
        raise DraftNotFound
    return draft


async def pending_edit(session: AsyncSession, user: User) -> AssistantDraft | None:
    since = datetime.now(timezone.utc) - EDIT_TIMEOUT
    return await session.scalar(
        select(AssistantDraft)
        .where(AssistantDraft.user_id == user.id, AssistantDraft.awaiting.is_not(None), AssistantDraft.updated_at >= since)
        .order_by(AssistantDraft.updated_at.desc())
        .limit(1)
    )


async def begin_edit(session: AsyncSession, user: User, draft_id: int, index: int, field: str) -> dict:
    draft = await get_draft(session, user, draft_id)
    if field not in EDIT_PROMPTS or not 0 <= index < len(draft.items):
        raise DraftNotFound
    await session.execute(
        AssistantDraft.__table__.update().where(AssistantDraft.user_id == user.id, AssistantDraft.id != draft.id).values(awaiting=None)
    )
    draft.awaiting = {"index": index, "field": field}
    await session.commit()
    return {"prompt": EDIT_PROMPTS[field], "title": draft.items[index]["title"], "field": field, "index": index}


def edit_item(item: dict, field: str, value: str, now: datetime) -> tuple[dict | None, str | None]:
    """The item with one field changed from free text, or an error message."""
    item = dict(item)
    if field == "title":
        title = " ".join(value.split())[:300]
        if not title:
            return None, "Название не может быть пустым"
        item["title"] = title
    elif field == "date":
        parsed = dates.parse(value, now)
        if parsed.date is None and parsed.rrule is None:
            return None, "Не поняла дату. Напишите, например: «завтра», «в пятницу», «через 2 дня», «15 октября»"
        current_time = time.fromisoformat(item["time"]) if item.get("time") else None
        if parsed.time is not None:
            item["time"], item["end_time"] = parsed.time.strftime("%H:%M"), parsed.end_time.strftime("%H:%M") if parsed.end_time else None
            current_time = parsed.time
        if parsed.rrule:
            item["rrule"] = parsed.rrule
            first = dates.first_occurrence(parsed.rrule, datetime.combine(parsed.date or now.date(), current_time or time.min, now.tzinfo), now)
            item["date"] = (first.date() if first else parsed.date or now.date()).isoformat()
        else:
            item["rrule"] = None
            item["date"] = parsed.date.isoformat()
    elif field == "time":
        if NO_TIME_WORDS.match(value):
            item["time"] = item["end_time"] = None
            return item, None
        candidate = value.strip()
        if re.match(r"^\d", candidate):
            candidate = ("с " if re.search(r"\d\s*(?:-|–|—|до)\s*\d", candidate) else "в ") + candidate
        start, end, _ = dates.parse_time(candidate)
        if start is None:
            return None, "Не поняла время. Напишите, например: «18:00», «в 9 утра», «с 10 до 12» или «без времени»"
        item["time"] = start.strftime("%H:%M")
        item["end_time"] = end.strftime("%H:%M") if end else None
    return item, None


async def apply_edit_text(session: AsyncSession, user: User, draft: AssistantDraft, text: str, tz: ZoneInfo) -> dict:
    awaiting = draft.awaiting or {}
    if text.strip().lower() in CANCEL_WORDS:
        draft.awaiting = None
        await session.commit()
        return proposal(draft, tz, note="Изменение отменено")
    index, field = awaiting.get("index"), awaiting.get("field")
    if not isinstance(index, int) or not 0 <= index < len(draft.items):
        draft.awaiting = None
        await session.commit()
        return proposal(draft, tz)
    item, error = edit_item(draft.items[index], field, text, datetime.now(tz))
    if error:
        return {"kind": "edit_error", "text": error, "draft_id": draft.id, "awaiting": awaiting}
    items = list(draft.items)
    items[index] = item
    draft.items = items
    draft.awaiting = None
    reply = proposal(draft, tz, note="Изменено ✓")
    await update_draft_messages(session, user, draft.id, reply)
    await session.commit()
    return reply


def validate_item(raw: dict, now: datetime) -> dict | None:
    """An item edited in the web form: same fields as a draft item, validated."""
    try:
        day = date.fromisoformat(str(raw.get("date")))
        start = time.fromisoformat(raw["time"]) if raw.get("time") else None
        end = time.fromisoformat(raw["end_time"]) if raw.get("end_time") and start else None
    except (TypeError, ValueError):
        return None
    parsed = dates.Parsed(date=day, time=start, end_time=end, rrule=dates.valid_rrule(raw.get("rrule")))
    item = build_item(str(raw.get("title") or ""), parsed, now, raw)
    if item and not raw.get("rrule"):
        item["date"] = day.isoformat()  # an explicit date from the form is kept even if it is in the past
    return item


async def replace_items(session: AsyncSession, user: User, draft_id: int, items: list[dict]) -> dict:
    draft = await get_draft(session, user, draft_id)
    tz = tasks.local_tz(user)
    now = datetime.now(tz)
    cleaned = [item for item in (validate_item(raw, now) for raw in items[:MAX_DRAFT_ITEMS]) if item]
    if not cleaned:
        raise ValueError("Нужна хотя бы одна задача с названием и датой")
    draft.items = cleaned
    draft.awaiting = None
    reply = proposal(draft, tz)
    await update_draft_messages(session, user, draft.id, reply)
    await session.commit()
    return reply


async def remove_item(session: AsyncSession, user: User, draft_id: int, index: int) -> dict:
    draft = await get_draft(session, user, draft_id)
    tz = tasks.local_tz(user)
    if 0 <= index < len(draft.items):
        draft.items = [item for position, item in enumerate(draft.items) if position != index]
    draft.awaiting = None
    if not draft.items:
        await session.delete(draft)
        reply = {"kind": "cancelled", "text": "Черновик пуст — ничего не добавлено"}
    else:
        reply = proposal(draft, tz, note="Удалено из черновика")
    await update_draft_messages(session, user, draft_id, reply)
    await session.commit()
    return reply


async def confirm_draft(session: AsyncSession, user: User, draft_id: int) -> dict:
    draft = await get_draft(session, user, draft_id)
    tz = tasks.local_tz(user)
    items = list(draft.items)
    await session.delete(draft)
    await session.flush()
    firsts, ids = await tasks.create_tasks(session, user, items)
    counts: dict[str | None, int] = {}
    if any(event.series_id for event in firsts):
        rows = await session.execute(
            select(Event.series_id, func.count()).where(Event.series_id.in_([event.series_id for event in firsts if event.series_id])).group_by(Event.series_id)
        )
        counts = dict(rows.all())
    reply = {
        "kind": "created",
        "events": [event_view(event, tz, repeats=max(0, counts.get(event.series_id, 1) - 1)) for event in firsts],
        "event_ids": ids,
        "answer": None,
    }
    await update_draft_messages(session, user, draft_id, reply)
    await session.commit()
    return reply


async def cancel_draft(session: AsyncSession, user: User, draft_id: int) -> dict:
    draft = await get_draft(session, user, draft_id)
    await session.delete(draft)
    reply = {"kind": "cancelled", "text": "Хорошо, ничего не добавляю"}
    await update_draft_messages(session, user, draft_id, reply)
    await session.commit()
    return reply


# ---------- entry point ----------


def summarize(reply: dict) -> str:
    kind = reply["kind"]
    if kind == "created":
        return "Добавила в календарь: " + "; ".join(f"{event['title']} ({event['start']})" for event in reply["events"])
    if kind == "proposal":
        return "Предложила добавить: " + "; ".join(f"{event['title']} ({event['start']})" for event in reply["events"])
    if kind == "agenda":
        count = sum(len(day["events"]) for day in reply["days"])
        return f"Показала события ({reply['title']}): {count}"
    return reply.get("text") or "Не нашла событий в сообщении"


async def extract_items(text: str, tz: ZoneInfo, history: str) -> tuple[list[dict], str | None]:
    now = datetime.now(tz)
    if not settings.gigachat_credentials:
        items = local_items(text, now)
        if not items:
            raise AssistantUnavailable
        return items, None
    from services.gigachat import GigaChatClient

    try:
        result = await GigaChatClient().process_message(text, str(tz), history)
    except Exception as error:
        logger.exception("GigaChat request failed")
        items = local_items(text, now)
        if not items:
            raise AssistantUnavailable from error
        return items, None
    raw = [item for item in result.get("events", []) if isinstance(item, dict)]
    if history:
        raw = [item for item in raw if grounded(item, text)]
    items = [item for item in (normalize_item(item, text, now, len(raw) == 1) for item in raw) if item]
    if not items and not result.get("answer"):
        items = local_items(text, now)
    return items, result.get("answer")


async def handle_message(session: AsyncSession, user: User, text: str) -> dict:
    tz = tasks.local_tz(user)
    text = text.strip()[:50000]
    history = await recent_context(session, user.id)
    draft = await pending_edit(session, user)
    if draft:
        reply = await apply_edit_text(session, user, draft, text, tz)
    elif is_question(text):
        reply = await search(session, user, text, tz)
    else:
        items, answer = await extract_items(text, tz, history)
        if items:
            reply = proposal(await create_draft(session, user, items), tz, answer=answer)
        else:
            if not answer and settings.gigachat_credentials:
                from services.gigachat import GigaChatClient

                try:
                    calendar = await calendar_context(session, user, tz)
                    answer = await GigaChatClient().chat_reply(text, str(tz), history, user.name, calendar)
                except Exception:
                    logger.exception("GigaChat chat reply failed")
            reply = {"kind": "answer", "text": answer} if answer else {"kind": "nothing"}
    await remember(session, user.id, "user", text)
    await remember(session, user.id, "assistant", summarize(reply), reply)
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
