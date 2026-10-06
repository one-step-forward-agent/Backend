"""Analysis of the user's day: the midday check-in and recommendations on the main screen.

Both use the onboarding profile (work days and hours, goals, spheres, tone of voice).
"""

import html
import json
import logging
import time as clock
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import Event, User
from app.services import tasks
from app.services.ru import MONTHS, WEEKDAYS_SHORT, plural

logger = logging.getLogger(__name__)

DAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
DEFAULT_DAY_END = time(21, 0)
BUSY_TASK_COUNT = 6
MAX_SUGGESTIONS = 3
PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "urgent": 3}
RECOMMENDATION_TTL = 30 * 60
_recommendation_cache: dict[int, tuple[float, str, list[dict]]] = {}

GREETINGS = {
    "supportive": "Как вы? Середина дня — хороший момент свериться с планом 🌿",
    "motivating": "Полдня позади — отличный момент ускориться 💪",
    "strict": "Контрольная точка дня.",
}


def _clock(value) -> time | None:
    try:
        return time.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def work_days(profile: dict) -> set[int]:
    days = profile.get("workDays") if isinstance(profile, dict) else None
    found = {DAY_NAMES.index(day) for day in days or [] if day in DAY_NAMES}
    return found or set(range(7))


def day_end(profile: dict, day: date) -> time:
    profile = profile if isinstance(profile, dict) else {}
    per_day = profile.get("perDayWorkHours") or {}
    hours = per_day.get(DAY_NAMES[day.weekday()]) if isinstance(per_day, dict) else None
    end = _clock((hours or {}).get("to")) if isinstance(hours, dict) else None
    end = end or _clock(profile.get("workHoursTo"))
    return end if end and day.weekday() in work_days(profile) else DEFAULT_DAY_END


def load_minutes(events: list[Event], now: datetime) -> int:
    total = 0
    for event in events:
        if event.all_day:
            total += tasks.UNTIMED_TASK_MINUTES
        else:
            total += max(0, int((event.end_at - max(event.start_at, now)).total_seconds() // 60))
    return total


def suggest_moves(remaining: list[Event], overloaded_by: int) -> list[Event]:
    """Tasks that are easiest to move: untimed first, then lower priority, then later ones; urgent stays."""
    movable = [event for event in remaining if event.priority != "urgent" and not event.series_id]
    movable.sort(key=lambda event: (not event.all_day, PRIORITY_RANK.get(event.priority, 1), -event.start_at.timestamp()))
    chosen, freed = [], 0
    for event in movable:
        if len(chosen) >= MAX_SUGGESTIONS or (chosen and freed >= overloaded_by):
            break
        chosen.append(event)
        freed += tasks.UNTIMED_TASK_MINUTES if event.all_day else int((event.end_at - event.start_at).total_seconds() // 60)
    return chosen


async def target_day(session: AsyncSession, user: User, tz: ZoneInfo, today: date) -> date:
    """The least busy of the next working days."""
    profile = user.profile or {}
    days = [today + timedelta(days=offset) for offset in range(1, 8) if (today + timedelta(days=offset)).weekday() in work_days(profile)][:3]
    days = days or [today + timedelta(days=1)]
    start = datetime.combine(days[0], time.min, tz)
    events = await tasks.events_between(session, user, start, datetime.combine(days[-1] + timedelta(days=1), time.min, tz))
    counts = {day: 0 for day in days}
    for event in events:
        local = event.start_at.astimezone(tz).date()
        if local in counts:
            counts[local] += 1
    return min(days, key=lambda day: (counts[day], day))


def day_text(day: date, today: date) -> str:
    if day == today + timedelta(days=1):
        return "завтра"
    return f"{WEEKDAYS_SHORT[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


async def checkin(session: AsyncSession, user: User, now: datetime) -> tuple[str, dict] | None:
    """Text and button payload for the midday check-in, or None when nothing is left for today."""
    tz = now.tzinfo
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today, time.min, tz), datetime.combine(today + timedelta(days=1), time.min, tz))
    remaining = [event for event in events if event.completed_at is None and (event.all_day or event.end_at > now)]
    if not remaining:
        return None
    profile = user.profile or {}
    end = datetime.combine(today, day_end(profile, today), tz)
    available = max(0, int((end - now).total_seconds() // 60))
    load = load_minutes(remaining, now)
    overloaded = load > available or len(remaining) >= BUSY_TASK_COUNT
    suggestions = suggest_moves(remaining, max(load - available, 1))
    target = await target_day(session, user, ZoneInfo(str(tz)), today)
    done = sum(event.completed_at is not None for event in events)

    tone = profile.get("toneOfVoice") if isinstance(profile, dict) else None
    lines = [f"🕐 <b>{GREETINGS.get(tone, 'Как успеваете?')}</b>", ""]
    if done:
        lines.append(f"Уже сделано: {done} из {len(events)} ✅")
    count = len(remaining)
    lines.append(f"Осталось на сегодня — {count} {plural(count, 'задача', 'задачи', 'задач')}:")
    for event in remaining[:8]:
        when = "без времени" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
        lines.append(f"• <code>{when}</code> {html.escape(event.title)}")
    if count > 8:
        lines.append(f"• …и ещё {count - 8}")
    lines.append("")
    hours, minutes = divmod(load, 60)
    load_text = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    if overloaded:
        lines.append(f"⚠️ По оценке это примерно {load_text}, а до конца дня ({end:%H:%M}) остаётся меньше. Похоже, всё не успеть.")
    else:
        lines.append(f"По оценке это примерно {load_text} — до {end:%H:%M} должно хватить времени.")
    if suggestions:
        names = ", ".join(f"«{html.escape(event.title)}»" for event in suggestions)
        lines.append(f"Если не успеваете, могу перенести {names} на {day_text(target, today)} — там свободнее.")
    lines.append("Успеваете?")
    payload = {"event_ids": [event.id for event in suggestions], "target": target.isoformat(), "target_label": day_text(target, today)}
    return "\n".join(lines), payload


# ---------- recommendations on the main screen ----------


def free_windows(events: list[Event], now: datetime, end: datetime, minimum: int = 45) -> list[tuple[datetime, datetime]]:
    windows, cursor = [], now
    for event in sorted((event for event in events if not event.all_day and event.end_at > now), key=lambda item: item.start_at):
        if (event.start_at - cursor).total_seconds() >= minimum * 60:
            windows.append((cursor, event.start_at))
        cursor = max(cursor, event.end_at)
    if (end - cursor).total_seconds() >= minimum * 60:
        windows.append((cursor, end))
    return windows


async def facts(session: AsyncSession, user: User, now: datetime) -> dict:
    tz = now.tzinfo
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today, time.min, tz), datetime.combine(today + timedelta(days=1), time.min, tz))
    yesterday = await tasks.events_between(session, user, datetime.combine(today - timedelta(days=3), time.min, tz), datetime.combine(today, time.min, tz))
    stats = await tasks.daily_stats(session, user, 7)
    profile = user.profile or {}
    end = datetime.combine(today, day_end(profile, today), tz)
    remaining = [event for event in events if event.completed_at is None and (event.all_day or event.end_at > now)]
    windows = free_windows(events, now, end)
    return {
        "now": now.strftime("%H:%M"),
        "day_end": end.strftime("%H:%M"),
        "today_total": len(events),
        "today_done": sum(event.completed_at is not None for event in events),
        "remaining_titles": [event.title for event in remaining[:10]],
        "untimed": [event.title for event in remaining if event.all_day][:5],
        "load_minutes": load_minutes(remaining, now),
        "free_windows": [f"{start:%H:%M}–{finish:%H:%M}" for start, finish in windows[:3]],
        "overdue": [event.title for event in yesterday if event.completed_at is None][:5],
        "week_percent": stats["percent"],
        "week_total": stats["total"],
        "streak": stats["streak"],
        "goals": [str(goal) for goal in profile.get("goals") or []][:5],
        "spheres": [str(sphere.get("name")) for sphere in profile.get("spheres") or [] if isinstance(sphere, dict)][:6],
        "tone": profile.get("toneOfVoice"),
    }


def rule_recommendations(data: dict) -> list[dict]:
    found = []
    if data["overdue"]:
        found.append({"kind": "warning", "title": "Хвосты с прошлых дней", "text": f"Не отмечены: {', '.join(data['overdue'][:3])}. Перенесите их на сегодня или завтра."})
    if data["untimed"] and data["free_windows"]:
        found.append({"kind": "info", "title": "Есть свободное окно", "text": f"{data['free_windows'][0]} — подходящее время для «{data['untimed'][0]}»."})
    available = max(0, (int(data["day_end"][:2]) * 60 + int(data["day_end"][3:])) - (int(data["now"][:2]) * 60 + int(data["now"][3:])))
    if data["load_minutes"] > available and data["remaining_titles"]:
        found.append({"kind": "warning", "title": "Плотный день", "text": "Задач больше, чем времени до конца дня. Часть можно перенести на завтра."})
    if data["week_total"] and data["week_percent"] < 50:
        found.append({"kind": "info", "title": "Меньше задач — больше результата", "text": f"За неделю выполнено {data['week_percent']}% задач. Попробуйте планировать 3–5 главных дел в день."})
    if data["streak"] >= 2:
        found.append({"kind": "success", "title": f"Серия {data['streak']} дн.", "text": "Все задачи выполнены несколько дней подряд — так держать!"})
    if not found:
        if data["today_total"] == 0:
            found.append({"kind": "info", "title": "День пока свободен", "text": "Напишите в чат планы на сегодня — я разложу их по времени."})
        else:
            found.append({"kind": "success", "title": "План под контролем", "text": "Нагрузка на сегодня в норме. Не забудьте отмечать выполненные задачи."})
    return found[:2]


async def recommendations(session: AsyncSession, user: User) -> list[dict]:
    """Up to two short recommendations; written by GigaChat from the facts, with rule-based fallback."""
    now = datetime.now(tasks.local_tz(user))
    data = await facts(session, user, now)
    key = json.dumps({k: v for k, v in data.items() if k != "now"}, ensure_ascii=False, sort_keys=True)
    cached = _recommendation_cache.get(user.id)
    if cached and cached[1] == key and clock.monotonic() - cached[0] < RECOMMENDATION_TTL:
        return cached[2]
    found = rule_recommendations(data)
    if settings.gigachat_credentials:
        from services.gigachat import GigaChatClient

        try:
            generated = await GigaChatClient().recommendations(data)
            if generated:
                found = generated
        except Exception:
            logger.exception("GigaChat recommendations failed")
    _recommendation_cache[user.id] = (clock.monotonic(), key, found)
    return found
