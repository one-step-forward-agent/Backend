from datetime import date, datetime, timedelta

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOMINATIVE = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_ACCUSATIVE = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


RELATIVE_DAYS = {0: "Сегодня", 1: "Завтра", -1: "Вчера"}


def day_label(day: date, today: date) -> str:
    """"Сегодня, среда, 7 октября", "Завтра, четверг, 8 октября", then "Пятница, 9 октября"."""
    base = f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"
    if day.year != today.year:
        base += f" {day.year}"
    relative = RELATIVE_DAYS.get((day - today).days)
    return f"{relative}, {base}" if relative else base[0].upper() + base[1:]


def relative_day(day: date, today: date) -> str:
    if day == today:
        return "сегодня"
    if day == today + timedelta(days=1):
        return "завтра"
    return f"{WEEKDAYS_SHORT[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def time_range(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%H:%M}–{end:%H:%M}"
    return f"{start:%H:%M} – {end.day} {MONTHS[end.month - 1]} {end:%H:%M}"


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many
