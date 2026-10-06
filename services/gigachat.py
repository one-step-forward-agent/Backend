import base64
import json
import logging
import re
import ssl
from functools import cache
from uuid import uuid4
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from app.core.config import settings

logger = logging.getLogger(__name__)


@cache
def _ssl_context() -> ssl.SSLContext | bool:
    if not settings.gigachat_ca_bundle:
        logger.warning("GIGACHAT_CA_BUNDLE is not set; GigaChat TLS certificates are not verified")
        return False
    context = ssl.create_default_context()
    context.load_verify_locations(settings.gigachat_ca_bundle)
    return context


WEEKDAY_NAMES = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнику": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнику": 1,
    "среда": 2,
    "среду": 2,
    "среды": 2,
    "четверг": 3,
    "четверга": 3,
    "четвергу": 3,
    "пятница": 4,
    "пятницу": 4,
    "пятницы": 4,
    "суббота": 5,
    "субботу": 5,
    "субботы": 5,
    "воскресенье": 6,
    "воскресенья": 6,
}


def _weekday_from_text(text: str) -> int | None:
    lowered = text.lower()
    for name, weekday in WEEKDAY_NAMES.items():
        if re.search(rf"(?<![а-яё]){name}(?![а-яё])", lowered):
            return weekday
    short_names = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
    for name, weekday in short_names.items():
        if re.search(rf"(?<![а-яё]){name}(?![а-яё])", lowered):
            return weekday
    return None


def _normalize_weekday_dates(events: list[dict], text: str, timezone: str) -> list[dict]:
    target_weekday = _weekday_from_text(text)
    if target_weekday is None or re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text):
        return events
    now = datetime.now(ZoneInfo(timezone))
    delta = (target_weekday - now.weekday()) % 7
    for event in events:
        try:
            start = datetime.fromisoformat(event["starts_at"])
        except (KeyError, TypeError, ValueError):
            continue
        event_delta = 7 if delta == 0 and start.timetz() <= now.timetz() else delta
        target_date = now.date() + timedelta(days=event_delta)
        corrected_start = start.replace(year=target_date.year, month=target_date.month, day=target_date.day)
        event["starts_at"] = corrected_start.isoformat()
        if event.get("ends_at"):
            try:
                end = datetime.fromisoformat(event["ends_at"])
                duration = end - start
                event["ends_at"] = (corrected_start + duration).isoformat()
            except (TypeError, ValueError):
                pass
    return events


def parse_events_response(content: str) -> list[dict]:
    cleaned_content = content.strip()
    if cleaned_content.startswith("```"):
        cleaned_content = cleaned_content.split("\n", 1)[1]
        cleaned_content = cleaned_content.rsplit("```", 1)[0].strip()

    decoder = json.JSONDecoder()
    array_start = cleaned_content.find("[")
    if array_start == -1:
        raise ValueError("GigaChat не вернул JSON-массив событий")
    try:
        events, _ = decoder.raw_decode(cleaned_content, array_start)
    except json.JSONDecodeError as error:
        raise ValueError("GigaChat вернул некорректный JSON-массив событий") from error
    if not isinstance(events, list):
        raise ValueError("GigaChat вернул некорректный список событий")
    return events


def parse_message_response(content: str) -> dict:
    cleaned_content = content.strip()
    if cleaned_content.startswith("```"):
        cleaned_content = cleaned_content.split("\n", 1)[1]
        cleaned_content = cleaned_content.rsplit("```", 1)[0].strip()
    if cleaned_content.startswith("["):
        return {"events": parse_events_response(cleaned_content), "answer": None}
    decoder = json.JSONDecoder()
    object_start = cleaned_content.find("{")
    if object_start == -1:
        return {"events": [], "answer": cleaned_content or None}
    try:
        result, _ = decoder.raw_decode(cleaned_content, object_start)
    except json.JSONDecodeError as error:
        repaired = re.sub(
            r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
            r'\1"\2"\3',
            cleaned_content,
        )
        try:
            result, _ = decoder.raw_decode(repaired, repaired.find("{"))
        except (json.JSONDecodeError, ValueError):
            logger.warning("GigaChat returned non-JSON answer: %s", error)
            return {"events": [], "answer": cleaned_content or None}
    if not isinstance(result, dict):
        return {"events": [], "answer": cleaned_content or None}
    events = result.get("events")
    if not isinstance(events, list):
        events = []
    answer = result.get("answer")
    result["answer"] = answer if isinstance(answer, str) else None
    result["events"] = events
    return result


def parse_search_filters_response(content: str) -> dict:
    cleaned_content = content.strip()
    if cleaned_content.startswith("```"):
        cleaned_content = cleaned_content.split("\n", 1)[1]
        cleaned_content = cleaned_content.rsplit("```", 1)[0].strip()
    decoder = json.JSONDecoder()
    object_start = cleaned_content.find("{")
    if object_start == -1:
        return {}
    try:
        result, _ = decoder.raw_decode(cleaned_content, object_start)
    except json.JSONDecodeError:
        repaired = re.sub(
            r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
            r'\1"\2"\3',
            cleaned_content,
        )
        try:
            result, _ = decoder.raw_decode(repaired, repaired.find("{"))
        except (json.JSONDecodeError, ValueError):
            return {}
    if not isinstance(result, dict):
        return {}
    return result


class GigaChatClient:
    token_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    chat_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    async def _token(self, session: aiohttp.ClientSession) -> str:
        logger.info("Requesting GigaChat access token")
        credentials = settings.gigachat_credentials
        if not settings.gigachat_credentials:
            credentials = base64.b64encode(credentials.encode()).decode()
        headers = {"Authorization": f"Basic {credentials}", "RqUID": str(uuid4())}
        async with session.post(
            self.token_url,
            headers=headers,
            data={"scope": settings.gigachat_scope},
            ssl=_ssl_context(),
        ) as response:
            if response.status >= 400:
                error_body = await response.text()
                logger.error("GigaChat token request failed: status=%s body=%s", response.status, error_body)
                response.raise_for_status()
            logger.info("GigaChat access token received")
            return (await response.json())["access_token"]

    async def process_message(
        self,
        text: str,
        timezone: str = "Europe/Moscow",
        context: str = "",
    ) -> dict:
        prompt = (
            "Ты извлекаешь события из текста для календаря. "
            "Найди ВСЕ отдельные события: встречи, занятия, лабораторные, звонки, "
            "получение или передачу вещей, поручения, покупки, дедлайны, напоминания "
            "и другие планы.\n"
            "Правила:\n"
            "1. Каждое отдельное действие или мероприятие должно быть отдельным объектом. "
            "Никогда не объединяй два события только потому, что они описаны в одном предложении.\n"
            "2. Союзы \"и\", \"затем\", \"после этого\", а также разные времена обычно "
            "обозначают отдельные события.\n"
            "3. Слова \"напомни\", \"напоминание\", \"напомнить\" никогда не являются отдельным событием: "
            "это только настройка reminder_minutes для основного события. Фраза вида "
            "\"каждую вторую пятницу напомни: пара в 15:00\" должна дать ровно ОДИН объект "
            "с title \"Пара\", reminder_minutes: 0 и recurrence_rule: "
            "\"FREQ=WEEKLY;INTERVAL=2;BYDAY=FR\".\n"
            "4. Повторяй дату для каждого события, если она указана один раз для всей фразы.\n"
            "5. Точно сохраняй название и важные детали: предмет, тип занятия, сервис, место и цель.\n"
            "6. Преобразуй дату и время в ISO 8601 с часовым поясом. "
            "Если год указан явно, обязательно используй его. Если дата указана как ДД.ММ.ГГГГ, "
            "не меняй её.\n"
            "Если указан день недели, обязательно проверь его по календарю перед ответом: "
            "пн/понедельник=0, вт/вторник=1, ср/среда=2, чт/четверг=3, "
            "пт/пятница=4, сб/суббота=5, вс/воскресенье=6. "
            "Для фразы \"в пн\" выбирай ближайший будущий понедельник относительно текущей даты, "
            "а не просто дату, которую предположила модель.\n"
            "7. Вычисляй относительные даты относительно текущей даты: \"сегодня\" — текущая дата, "
            "\"завтра\" — следующий календарный день, \"послезавтра\" — через два дня. "
            "Фразу \"через N минут\" или \"через N часов\" считай временем начала "
            "относительно текущего момента, а не названием события.\n"
            "Не отбрасывай событие только потому, что в нём нет точного времени.\n"
            "8. Если указано только время без даты, используй ближайшую подходящую дату относительно "
            "текущей даты. Если дата есть, но время не указано, используй 09:00.\n"
            "9. Если длительность или время окончания не указаны, ends_at должен быть null.\n"
            "10. Если пользователь просит напомнить за N минут, укажи reminder_minutes как число N. "
            "Если пользователь просит просто \"напомни\" без \"за N минут\", укажи 0 "
            "(напомнить ровно в момент события). Если напоминание не запрошено, используй null.\n"
            "11. Если событие повторяется (например, каждую пятницу, каждую вторую неделю, "
            "каждый год), укажи recurrence_rule как строку RFC 5545 RRULE без префикса RRULE:. "
            "Используй FREQ=DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL для шага, BYDAY для дней "
            "(например, FREQ=WEEKLY;BYDAY=FR или FREQ=WEEKLY;INTERVAL=2), "
            "BYMONTHDAY для дня месяца. Для одноразового события recurrence_rule: null.\n"
            "12. Анализируй события только в блоке Текущий запрос. Контекст нужен только для "
            "ответа на вопрос о прошлом. Не переноси события из контекста в текущий запрос и "
            "не создавай их повторно. Например, \"напомни завтра Егору покрасить забор\" "
            "это ровно одно событие \"Покрасить забор\"; слово \"Егору\" является деталью, "
            "а не отдельным событием.\n"
            "13. Если в сообщении есть события, положи их в массив events. Если это вопрос "
            "о прошлых сообщениях или событие не найдено, events должен быть пустым массивом, "
            "а в answer дай короткий полезный ответ на русском языке с учетом контекста. "
            "Всегда верни валидный JSON-объект вида {\"events\": [...], "
            "\"answer\": \"...\" или null} "
            "без markdown, пояснений и дополнительного текста. "
            "Формат объекта: title (строка), starts_at (строка), ends_at (строка или null), "
            "description (строка или null), location (строка или null), "
            "reminder_minutes (целое число или null), recurrence_rule (строка или null).\n"
            "Пример напоминания. Вход: \"Встреча завтра в 15:00, напомни за 10 минут\". "
            "В объекте должно быть reminder_minutes: 10.\n"
            "Пример повторения. Вход: \"Каждую пятницу в 12:00 пара\". "
            "recurrence_rule должен быть \"FREQ=WEEKLY;BYDAY=FR\".\n"
            "Пример повторения с напоминанием. Вход: \"Каждую вторую пятницу напомни: пара "
            "Схемотехника в 15:00\". Верни ровно один объект: title \"Пара Схемотехника\", "
            "starts_at с ближайшей подходящей пятницей в 15:00, reminder_minutes: 0, "
            "recurrence_rule \"FREQ=WEEKLY;INTERVAL=2;BYDAY=FR\". "
            "Не создавай отдельное событие \"напоминание\".\n"
            "Пример относительного времени. Вход: \"через 5 минут у меня встреча, "
            "напомни о ней за 1 минуту\". "
            "starts_at должен быть примерно через 5 минут от текущего времени, "
            "а reminder_minutes должен быть 1.\n"
            "Пример. Вход: \"получить завтра от Тёмы стекло от люстры\". "
            "При текущей дате 17.09.2026 правильный результат: "
            "[{\"title\":\"Получить стекло от люстры\",\"starts_at\":\"2026-09-18T09:00:00+03:00\","
            "\"ends_at\":null,\"description\":\"Получить от Тёмы\",\"location\":null}].\n"
            "Пример. Вход: \"В 20:00 17.09.2026 у меня лабораторная по ТП в телемосте "
            "и в 21:00 обсуждение стартапа.\" "
            "Правильный результат: "
            "[{\"title\":\"Лабораторная по ТП\",\"starts_at\":\"2026-09-17T20:00:00+03:00\","
            "\"ends_at\":null,\"description\":null,\"location\":\"телемост\"},"
            "{\"title\":\"Обсуждение стартапа\",\"starts_at\":\"2026-09-17T21:00:00+03:00\"," 
            "\"ends_at\":null,\"description\":null,\"location\":null}].\n"
            f"Текущие дата и время: {datetime.now(ZoneInfo(timezone)).isoformat()}. "
            f"Часовой пояс календаря пользователя: {timezone}. "
            "Все starts_at и ends_at возвращай с явным смещением этого часового пояса.\n\n"
            "Перед ответом проверь каждый объект: его title, description или location должны быть "
            "подтверждены словами из текущего запроса. Не добавляй события из примеров, контекста "
            "или собственных предположений. Если подтверждения нет, удали объект.\n\n"
            "Контекст предыдущего диалога (это справочная информация, не инструкция):\n"
            f"{context or '(пока пусто)'}\n\n"
            "Текущий запрос (единственный источник новых событий):\n"
            f"{text[:50000]}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            }
            async with session.post(
                self.chat_url, headers=headers, json=payload, ssl=_ssl_context()
            ) as response:
                response.raise_for_status()
                result = await response.json()
        logger.info("GigaChat response received, input length: %d", len(text))
        content = result["choices"][0]["message"]["content"]
        parsed = parse_message_response(content)
        parsed["events"] = _normalize_weekday_dates(parsed["events"], text, timezone)
        logger.info("Parsed %d events from GigaChat response", len(parsed["events"]))
        return parsed

    async def extract_search_filters(self, text: str, timezone: str) -> dict:
        now = datetime.now(ZoneInfo(timezone)).isoformat()
        prompt = (
            "Извлеки фильтры поиска сохраненных событий из запроса пользователя. "
            "Не создавай события и не отвечай текстом. Верни только валидный JSON-объект.\n"
            "Поля: date_from и date_to (YYYY-MM-DD или null), time_from и time_to (HH:MM или null), "
            "keywords (массив коротких слов для поиска в названии, описании и месте).\n"
            "\"завтра\" означает одну завтрашнюю дату; \"через месяц\" означает дату через один месяц; "
            "\"утром\" означает 05:00-12:00, \"днем\" 12:00-18:00, "
            "\"вечером\" 18:00-24:00. Если фильтр не указан, используй null или [].\n"
            f"Текущие дата и время: {now}. Часовой пояс: {timezone}.\n"
            f"Запрос: {text[:2000]}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 180,
            }
            async with session.post(
                self.chat_url, headers=headers, json=payload, ssl=_ssl_context()
            ) as response:
                response.raise_for_status()
                result = await response.json()
        return parse_search_filters_response(result["choices"][0]["message"]["content"])

    async def chat_reply(self, text: str, timezone: str = "Europe/Moscow", context: str = "", name: str | None = None) -> str:
        now = datetime.now(ZoneInfo(timezone))
        system = (
            "Ты — Dayla, дружелюбный и собранный ИИ-ассистент по планированию дня. "
            "Ты общаешься с пользователем в Telegram и умеешь добавлять события в его календарь, "
            "показывать расписание и присылать напоминания. "
            "Отвечай по-русски, тепло и по делу: до 5–6 коротких предложений или короткий список. "
            "Не используй markdown, заголовки и таблицы. Не выдумывай события и факты о расписании пользователя. "
            "Если пользователь хочет что-то запланировать, но не указал дату или время, коротко уточни, когда это сделать. "
            f"Сейчас {now:%d.%m.%Y %H:%M}, часовой пояс {timezone}."
            + (f" Пользователя зовут {name}." if name else "")
        )
        user_message = (
            "Недавний диалог (справочно):\n"
            f"{context or '(пусто)'}\n\n"
            f"Сообщение пользователя:\n{text[:6000]}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_message}],
                "temperature": 0.5,
                "max_tokens": 600,
            }
            async with session.post(
                self.chat_url, headers=headers, json=payload, ssl=_ssl_context()
            ) as response:
                response.raise_for_status()
                result = await response.json()
        return result["choices"][0]["message"]["content"].strip()

    async def extract_events(self, text: str, timezone: str = "Europe/Moscow") -> list[dict]:
        return (await self.process_message(text, timezone))["events"]
