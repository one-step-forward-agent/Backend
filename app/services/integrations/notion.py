import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.services.integrations.base import ConfigField, EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _database_id(value: str) -> str:
    match = re.search(r"([0-9a-f]{32})", value.replace("-", "").lower())
    if not match:
        raise IntegrationError("Не удалось распознать ID базы Notion")
    raw = match.group(1)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _parse_date(value: str, tz: ZoneInfo) -> tuple[datetime, bool]:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, tz), True
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)), False


class NotionIntegration(IntegrationProvider):
    slug = "notion"
    title = "Notion"
    description = "Внутренняя интеграция Notion. Откройте базу → ··· → Connections и добавьте интеграцию."
    fields = [
        ConfigField("token", "Internal integration secret", type="password", secret=True, placeholder="ntn_…", help="notion.so/my-integrations"),
        ConfigField("database_id", "Ссылка или ID базы данных", placeholder="https://www.notion.so/…"),
        ConfigField("date_property", "Свойство с датой", default="Date", required=False),
    ]

    @property
    def database_id(self) -> str:
        return _database_id(self.config["database_id"])

    @property
    def date_property(self) -> str:
        return self.config.get("date_property") or "Date"

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.secrets['token']}", "Notion-Version": NOTION_VERSION}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, f"{API_URL}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise IntegrationError("Notion недоступен") from error
        if response.status_code == 401:
            raise IntegrationError("Notion отклонил токен интеграции")
        if response.status_code == 404:
            raise IntegrationError("База не найдена — добавьте интеграцию в Connections этой базы")
        if response.is_error:
            raise IntegrationError(f"Notion: {response.json().get('message', response.status_code)}")
        return response.json()

    async def _schema(self) -> dict:
        database = await self._request("GET", f"/databases/{self.database_id}")
        properties = database.get("properties", {})
        if self.date_property not in properties or properties[self.date_property].get("type") != "date":
            raise IntegrationError(f"В базе нет свойства-даты «{self.date_property}»")
        return database

    async def verify(self) -> str:
        database = await self._schema()
        title = "".join(part.get("plain_text", "") for part in database.get("title", [])) or "без названия"
        return f"База «{title}»"

    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        tz = ZoneInfo(self.context.timezone)
        body = {
            "filter": {
                "and": [
                    {"property": self.date_property, "date": {"on_or_after": start.date().isoformat()}},
                    {"property": self.date_property, "date": {"on_or_before": end.date().isoformat()}},
                ]
            },
            "page_size": 100,
        }
        items = []
        for _ in range(5):
            data = await self._request("POST", f"/databases/{self.database_id}/query", json=body)
            for page in data.get("results", []):
                properties = page.get("properties", {})
                value = (properties.get(self.date_property) or {}).get("date") or {}
                if not value.get("start"):
                    continue
                start_at, all_day = _parse_date(value["start"], tz)
                if value.get("end"):
                    end_at, _ = _parse_date(value["end"], tz)
                    end_at = end_at + timedelta(days=1) if all_day else end_at
                else:
                    end_at = start_at + (timedelta(days=1) if all_day else timedelta(hours=1))
                title_prop = next((prop for prop in properties.values() if prop.get("type") == "title"), {})
                title = "".join(part.get("plain_text", "") for part in title_prop.get("title", [])) or "(без названия)"
                items.append(RemoteItem(external_id=page["id"], title=title, start_at=start_at, end_at=end_at, all_day=all_day, url=page.get("url")))
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
        return items

    async def push_event(self, event: EventPayload) -> PushResult:
        database = await self._schema()
        title_name = next((name for name, prop in database["properties"].items() if prop.get("type") == "title"), "Name")
        tz = ZoneInfo(event.timezone)
        if event.all_day:
            date_value = {"start": event.start_at.astimezone(tz).date().isoformat()}
        else:
            date_value = {"start": event.start_at.isoformat(), "end": event.end_at.isoformat()}
        page = {
            "parent": {"database_id": self.database_id},
            "properties": {
                title_name: {"title": [{"text": {"content": event.title[:2000]}}]},
                self.date_property: {"date": date_value},
            },
        }
        if event.description:
            page["children"] = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": event.description[:2000]}}]}}]
        result = await self._request("POST", "/pages", json=page)
        return PushResult(external_id=result["id"], url=result.get("url"))
