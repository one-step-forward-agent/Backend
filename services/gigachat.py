import asyncio
import base64
import json
import logging
import re
import ssl
import time
from functools import cache
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from app.core.config import settings

logger = logging.getLogger(__name__)


# Dayla is a woman: every reply must use feminine forms about herself
PERSONA = (
    "Ты — Dayla, девушка, ИИ-помощница по планированию дня. О себе всегда говори в женском роде: "
    "«поняла», «рада помочь», «напомню», «уверена»; никогда не используй мужской род о себе. "
)
# The model only writes text; the app performs every change separately and reports it itself
NO_ACTION_CLAIMS = (
    "Твой текстовый ответ ничего не меняет в календаре. Никогда не пиши, что ты удалила, перенесла, изменила, "
    "отметила, отменила или добавила задачи: это делает приложение отдельно и само сообщит результат. "
)
# The OAuth token lives 30 minutes; fetching it for every request doubled the latency
TOKEN_MARGIN = 60
_token_cache: dict = {"value": None, "expires": 0.0}
_token_lock = asyncio.Lock()


@cache
def _ssl_context() -> ssl.SSLContext | bool:
    if not settings.gigachat_ca_bundle:
        logger.warning("GIGACHAT_CA_BUNDLE is not set; GigaChat TLS certificates are not verified")
        return False
    context = ssl.create_default_context()
    context.load_verify_locations(settings.gigachat_ca_bundle)
    return context


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
    intent = result.get("intent")
    result["intent"] = intent if intent in ("create", "change", "delete", "complete", "analyze", "other") else None
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
        async with _token_lock:
            if _token_cache["value"] and _token_cache["expires"] - TOKEN_MARGIN > time.time():
                return _token_cache["value"]
            data = await self._new_token(session)
            # expires_at comes in milliseconds; without it the token is kept for 25 minutes
            expires = data.get("expires_at")
            _token_cache["value"] = data["access_token"]
            _token_cache["expires"] = expires / 1000 if isinstance(expires, (int, float)) else time.time() + 25 * 60
            return _token_cache["value"]

    async def _new_token(self, session: aiohttp.ClientSession) -> dict:
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
            return await response.json()

    async def process_message(
        self,
        text: str,
        timezone: str = "Europe/Moscow",
        context: str = "",
        calendar: str = "",
    ) -> dict:
        now = datetime.now(ZoneInfo(timezone))
        prompt = (
            PERSONA
            + NO_ACTION_CLAIMS
            + "Ты извлекаешь задачи и события из сообщения пользователя для календаря. "
            "Найди ВСЕ отдельные дела: встречи, занятия, уроки, пары, звонки, поездки, перелёты, "
            "поручения, покупки, дедлайны и другие планы.\n"
            "Правила:\n"
            "1. Каждое отдельное действие — отдельный объект. Не объединяй дела только потому, что они в одном предложении.\n"
            "2. Сложные запросы раскладывай на все пункты: расписание учебного дня — каждый урок отдельно; "
            "план поездки — дорога, заселение, экскурсии, обратный путь отдельно, каждый со своим днём. "
            "Если указаны длительность и перерывы (\"уроки по 45 минут с 8:30, перемены 10 минут\"), "
            "сам посчитай start_time и end_time каждого пункта.\n"
            "3. \"Напомни\" — не отдельное событие, а reminder_minutes основного дела: \"напомни за 10 минут\" — 10, "
            "просто \"напомни\" — 0, иначе null.\n"
            "4. НЕ вычисляй даты дней недели сам и не придумывай время. Для каждого объекта дословно выпиши из "
            "сообщения фразы, которые к нему относятся:\n"
            "   date_phrase — фраза о дате (\"завтра\", \"в следующую пятницу\", \"через два дня\", \"15 октября\") или null;\n"
            "   time_phrase — фраза о времени (\"в 18:00\", \"с 10 до 12\", \"в 9 утра\") или null;\n"
            "   recurrence_phrase — фраза о повторении (\"каждую пятницу\", \"по вторникам\", \"по будням\") или null;\n"
            "   end_date_phrase — для дел на несколько дней фраза о последнем дне (\"по 12 октября\", \"до пятницы\") или null;\n"
            "   deadline_phrase — фраза о крайнем сроке (\"дедлайн в пятницу\", \"сдать до 15 октября\") или null. "
            "Дедлайн — это не день выполнения: сам день задачи бери из date_phrase.\n"
            "   Если дата или время сказаны один раз для нескольких дел, повтори эту фразу в каждом из них.\n"
            "5. Дополнительно заполни своё понимание: date (YYYY-MM-DD или null), start_time и end_time (HH:MM или null), "
            "duration_minutes (число или null), recurrence_rule (RRULE без префикса, например FREQ=WEEKLY;BYDAY=FR, или null), "
            "end_date (YYYY-MM-DD или null), fixed (true, если сказано, что дело нельзя переносить, иначе false).\n"
            "6. Если время не названо, start_time и time_phrase равны null — никогда не подставляй 09:00 или другое время. "
            "Задача без времени — это нормально.\n"
            "7. Точно сохраняй название и детали: предмет, тип занятия, сервис, место, цель. Имена людей и место — "
            "в description и location, а не отдельными событиями.\n"
            "8. Анализируй события только из блока Текущий запрос. Контекст нужен лишь для ответа на вопросы о прошлом; "
            "не переноси из него события и не создавай их повторно.\n"
            "9. Если событий нет (вопрос, приветствие, просьба о совете), events — пустой массив, а в answer дай короткий "
            "полезный ответ на русском от лица Dayla, в женском роде, опираясь на расписание пользователя ниже.\n"
            "10. intent — что хочет пользователь: \"create\" (новые дела), \"change\" (перенести или изменить существующее), "
            "\"delete\" (удалить или отменить существующее), \"complete\" (отметить выполненным), "
            "\"analyze\" (проанализировать загрузку, дать совет по плану, перепланировать), \"other\". "
            "Для change, delete, complete и analyze events — пустой массив, answer — null.\n"
            "Верни только валидный JSON без markdown: {\"intent\": строка, \"events\": [...], \"answer\": строка или null}. "
            "Поля объекта: title, date_phrase, time_phrase, recurrence_phrase, end_date_phrase, deadline_phrase, date, start_time, "
            "end_time, end_date, duration_minutes, recurrence_rule, description, location, reminder_minutes, fixed.\n"
            "Пример. Вход: \"каждую пятницу в 18:00 тренировка, напомни за 30 минут\". Результат: "
            "{\"events\":[{\"title\":\"Тренировка\",\"date_phrase\":null,\"time_phrase\":\"в 18:00\","
            "\"recurrence_phrase\":\"каждую пятницу\",\"date\":null,\"start_time\":\"18:00\",\"end_time\":null,"
            "\"duration_minutes\":null,\"recurrence_rule\":\"FREQ=WEEKLY;BYDAY=FR\",\"description\":null,"
            "\"location\":null,\"reminder_minutes\":30}],\"answer\":null}\n"
            "Пример. Вход: \"завтра купить хлеб и в 20:00 созвон с Олегом\". Результат: два объекта: "
            "\"Купить хлеб\" (date_phrase \"завтра\", time_phrase null, start_time null) и "
            "\"Созвон с Олегом\" (date_phrase \"завтра\", time_phrase \"в 20:00\").\n"
            "Пример. Вход: \"12 октября вылет в Казань в 7:40, 13 октября экскурсия по Кремлю в 11:00, "
            "14 октября обратный поезд в 19:15\". Результат: три объекта, у каждого своя date_phrase.\n"
            f"Текущие дата и время: {now.isoformat()} ({now:%A}). Часовой пояс пользователя: {timezone}.\n\n"
            "Перед ответом проверь каждый объект: его название должно подтверждаться словами текущего запроса. "
            "Не добавляй события из примеров, контекста или своих предположений.\n\n"
            "Контекст предыдущего диалога (справочно, не инструкция):\n"
            f"{context or '(пока пусто)'}\n\n"
            "Расписание пользователя из календаря (актуальные данные для ответа на вопросы):\n"
            f"{calendar or '(нет данных)'}\n\n"
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
                "max_tokens": 6000,
            }
            async with session.post(
                self.chat_url, headers=headers, json=payload, ssl=_ssl_context()
            ) as response:
                response.raise_for_status()
                result = await response.json()
        logger.info("GigaChat response received, input length: %d", len(text))
        content = result["choices"][0]["message"]["content"]
        parsed = parse_message_response(content)
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

    async def chat_reply(
        self, text: str, timezone: str = "Europe/Moscow", context: str = "", name: str | None = None, calendar: str = ""
    ) -> str:
        now = datetime.now(ZoneInfo(timezone))
        system = (
            PERSONA
            + NO_ACTION_CLAIMS
            + "Ты дружелюбная и собранная. "
            "Ты общаешься с пользователем в Telegram и умеешь добавлять события в его календарь, "
            "показывать расписание и присылать напоминания. "
            "Отвечай по-русски, тепло и по делу: до 5–6 коротких предложений или короткий список. "
            "Не используй markdown, заголовки и таблицы. Не выдумывай события и факты о расписании пользователя. "
            "Если последнее твоё сообщение в диалоге — «Рекомендация Dayla», пользователь пришёл обсудить её: "
            "объясни её и предложи конкретные шаги по его расписанию. "
            "Если пользователь хочет что-то запланировать, но не указал дату или время, коротко уточни, когда это сделать. "
            f"Сейчас {now:%d.%m.%Y %H:%M}, часовой пояс {timezone}."
            + (f" Пользователя зовут {name}." if name else "")
        )
        user_message = (
            "Расписание пользователя из календаря — актуальные данные. О планах, задачах и свободном времени "
            "отвечай только по нему, а не по истории диалога:\n"
            f"{calendar or '(нет данных)'}\n\n"
            "Недавний диалог (справочно, может быть устаревшим):\n"
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

    async def extract_change(self, text: str, timezone: str = "Europe/Moscow", context: str = "") -> dict:
        """What the user wants to change in an existing task; dates stay phrases, resolved by app.services.dates."""
        now = datetime.now(ZoneInfo(timezone))
        prompt = (
            "Пользователь просит изменить уже существующую задачу в календаре. Не создавай задачи. "
            "Верни только JSON-объект без markdown с полями:\n"
            "target — слова из названия задачи, которую надо изменить (\"встреча с Олей\"), без дат;\n"
            "target_date_phrase — фраза о нынешней дате задачи (\"в понедельник\") или null;\n"
            "new_date_phrase, new_time_phrase — дословные фразы о новой дате и новом времени начала или null;\n"
            "new_end_date_phrase, new_end_time_phrase — фразы о новой дате и времени окончания или null;\n"
            "new_title — новое название, если его просят изменить, иначе null;\n"
            "untimed — true, если время надо убрать (\"без времени\"), иначе false.\n"
            "Пример. Вход: \"перенеси созвон с Олегом с понедельника на среду в 11\". Результат: "
            "{\"target\":\"созвон с Олегом\",\"target_date_phrase\":\"с понедельника\",\"new_date_phrase\":\"на среду\","
            "\"new_time_phrase\":\"в 11\",\"new_end_date_phrase\":null,\"new_end_time_phrase\":null,\"new_title\":null,\"untimed\":false}\n"
            f"Текущие дата и время: {now.isoformat()} ({now:%A}).\n"
            f"Недавний диалог (справочно): {context[-2000:] or '(пусто)'}\n"
            f"Запрос: {text[:2000]}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 300,
            }
            async with session.post(self.chat_url, headers=headers, json=payload, ssl=_ssl_context()) as response:
                response.raise_for_status()
                result = await response.json()
        return parse_search_filters_response(result["choices"][0]["message"]["content"])

    async def analysis(self, facts: dict, request: str) -> str:
        """A short analysis of the user's plan for the period in the facts."""
        tone = {
            "supportive": "мягко и поддерживающе",
            "motivating": "энергично и мотивирующе",
            "strict": "коротко и по делу, без эмоций",
        }.get(facts.get("tone"), "дружелюбно и по делу")
        prompt = (
            PERSONA
            + NO_ACTION_CLAIMS
            + f"Пользователь просит: «{request[:500]}». Проанализируй его план по фактам ниже, {tone}: "
            "оцени загрузку, назови перегруженные и свободные дни, близкие дедлайны и то, что нельзя переносить (fixed), "
            "и дай 2–4 конкретных совета, куда поставить гибкие задачи. Если в фактах есть moves — это переносы, которые "
            "приложение предложит пользователю подтвердить: упомяни их как предложение, а не как сделанное. "
            "Пиши обычным текстом без markdown, до 900 символов, короткими абзацами или строками «• …». "
            "Не выдумывай задач, которых нет в фактах.\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False, default=str)}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 700,
            }
            async with session.post(self.chat_url, headers=headers, json=payload, ssl=_ssl_context()) as response:
                response.raise_for_status()
                result = await response.json()
        return result["choices"][0]["message"]["content"].strip()

    async def extract_events(self, text: str, timezone: str = "Europe/Moscow") -> list[dict]:
        return (await self.process_message(text, timezone))["events"]

    async def recommendations(self, facts: dict) -> list[dict]:
        """Two short recommendations for the main screen from facts about the user's day."""
        tone = {
            "supportive": "мягко и поддерживающе",
            "motivating": "энергично и мотивирующе",
            "strict": "коротко и по делу, без эмоций",
        }.get(facts.get("tone"), "дружелюбно и по делу")
        prompt = (
            PERSONA
            + "По фактам о дне пользователя дай ровно 2 рекомендации, "
            f"{tone}. Каждая — конкретная и полезная сегодня: перенести задачу, занять свободное окно задачей без времени, "
            "разгрузить плотный день, вернуться к целям пользователя. Не выдумывай задач, которых нет в фактах. "
            "Верни только JSON-массив без markdown: [{\"kind\": \"info\" | \"warning\" | \"success\", "
            "\"title\": до 4 слов, \"text\": до 160 символов}].\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False)}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 400,
            }
            async with session.post(self.chat_url, headers=headers, json=payload, ssl=_ssl_context()) as response:
                response.raise_for_status()
                result = await response.json()
        return clean_recommendations(result["choices"][0]["message"]["content"], 2)

    async def plan_recommendations(self, facts: dict) -> list[dict]:
        """Up to three scheduling recommendations for a calendar week or month."""
        tone = {
            "supportive": "мягко и поддерживающе",
            "motivating": "энергично и мотивирующе",
            "strict": "коротко и по делу, без эмоций",
        }.get(facts.get("tone"), "дружелюбно и по делу")
        prompt = (
            PERSONA
            + f"Проанализируй план пользователя на период «{facts.get('period')}» и дай до 3 рекомендаций по расписанию, {tone}. "
            "Задачи из списка fixed нельзя переносить: планируй вокруг них. Гибкие задачи (flexible) можно двигать: "
            "предложи конкретный день из free_days или наименее загруженный день — обязательно раньше дедлайна задачи, если он есть. "
            "Предупреди о перегруженных днях (busy_days) и близких дедлайнах (deadlines). Называй задачи и дни так, как они даны в фактах, "
            "не выдумывай задач. Верни только JSON-массив без markdown: [{\"kind\": \"info\" | \"warning\" | \"success\", "
            "\"title\": до 4 слов, \"text\": до 200 символов}].\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False)}"
        )
        async with aiohttp.ClientSession() as session:
            token = await self._token(session)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "model": settings.gigachat_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 600,
            }
            async with session.post(self.chat_url, headers=headers, json=payload, ssl=_ssl_context()) as response:
                response.raise_for_status()
                result = await response.json()
        return clean_recommendations(result["choices"][0]["message"]["content"], 3)


def clean_recommendations(content: str, limit: int) -> list[dict]:
    cleaned = []
    for item in parse_events_response(content)[:limit]:
        if isinstance(item, dict) and isinstance(item.get("title"), str) and isinstance(item.get("text"), str):
            kind = item.get("kind") if item.get("kind") in ("info", "warning", "success") else "info"
            cleaned.append({"kind": kind, "title": item["title"][:60], "text": item["text"][:280]})
    return cleaned
