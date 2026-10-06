import hashlib
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.services.integrations.base import ConfigField, EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem, guarded_client

TASK_LINE = re.compile(r"^\s*[-*]\s+\[(?P<done>[ xX])\]\s+(?P<body>.+?)\s*$")
DUE_DATE = re.compile(r"(?:📅|\[?due::)\s*(?P<date>\d{4}-\d{2}-\d{2})\]?")
DUE_TIME = re.compile(r"⏰\s*(?P<time>\d{1,2}:\d{2})")


def task_id(body: str) -> str:
    return hashlib.sha1(body.strip().encode()).hexdigest()[:20]


def parse_tasks(markdown: str, tz: ZoneInfo) -> list[tuple[str, str, datetime, bool]]:
    tasks = []
    for line in markdown.splitlines():
        match = TASK_LINE.match(line)
        if not match or match.group("done") != " ":
            continue
        body = match.group("body")
        due = DUE_DATE.search(body)
        if not due:
            continue
        day = date.fromisoformat(due.group("date"))
        clock = DUE_TIME.search(body)
        title = DUE_TIME.sub("", DUE_DATE.sub("", body))
        title = re.sub(r"[⏫🔼🔽⏬🔺🔁➕⏳🛫✅]\s*\S*", "", title).strip() or "(без названия)"
        if clock:
            hours, minutes = map(int, clock.group("time").split(":"))
            tasks.append((task_id(body), title, datetime.combine(day, time(hours, minutes), tz), False))
        else:
            tasks.append((task_id(body), title, datetime.combine(day, time.min, tz), True))
    return tasks


class ObsidianIntegration(IntegrationProvider):
    slug = "obsidian"
    title = "Obsidian"
    description = (
        "Плагин Local REST API. Задачи с датой из выбранной заметки попадают в календарь, "
        "события Dayla добавляются туда же как задачи. Сервер Dayla должен видеть адрес плагина."
    )
    fields = [
        ConfigField("base_url", "Адрес Local REST API", type="url", default="https://127.0.0.1:27124"),
        ConfigField("api_key", "API key", type="password", secret=True, help="Settings → Local REST API → API Key"),
        ConfigField("tasks_file", "Заметка с задачами", default="Focus Day/Tasks.md"),
        ConfigField("verify_ssl", "Проверять SSL-сертификат", type="checkbox", required=False, default=False, help="У плагина самоподписанный сертификат"),
    ]

    @property
    def tasks_path(self) -> str:
        return "/vault/" + quote(self.config.get("tasks_file") or "Focus Day/Tasks.md")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.secrets['api_key']}", **kwargs.pop("headers", {})}
        try:
            async with guarded_client(timeout=15, verify=bool(self.config.get("verify_ssl"))) as client:
                response = await client.request(method, f"{self.config['base_url'].rstrip('/')}{path}", headers=headers, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise IntegrationError("Obsidian Local REST API недоступен — Obsidian запущен и адрес доступен с сервера?") from error
        if response.status_code in (401, 403):
            raise IntegrationError("Obsidian отклонил API key")
        return response

    async def verify(self) -> str:
        response = await self._request("GET", "/")
        data = response.json() if response.is_success else {}
        if not data.get("authenticated"):
            raise IntegrationError("Obsidian отклонил API key")
        version = (data.get("versions") or {}).get("obsidian", "")
        return f"Obsidian {version}".strip()

    async def _read_tasks(self) -> str:
        response = await self._request("GET", self.tasks_path, headers={"Accept": "text/markdown"})
        if response.status_code == 404:
            return ""
        if response.is_error:
            raise IntegrationError(f"Obsidian ответил {response.status_code}")
        return response.text

    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        tz = ZoneInfo(self.context.timezone)
        items = []
        for identifier, title, start_at, all_day in parse_tasks(await self._read_tasks(), tz):
            if not start <= start_at <= end:
                continue
            end_at = start_at + (timedelta(days=1) if all_day else timedelta(minutes=30))
            items.append(RemoteItem(external_id=identifier, title=title, start_at=start_at, end_at=end_at, all_day=all_day))
        return items

    async def push_event(self, event: EventPayload) -> PushResult:
        local = event.start_at.astimezone(ZoneInfo(event.timezone))
        title = " ".join(event.title.split())
        body = f"{title} 📅 {local:%Y-%m-%d}" if event.all_day else f"{title} 📅 {local:%Y-%m-%d} ⏰ {local:%H:%M}"
        existing = await self._read_tasks()
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        response = await self._request("POST", self.tasks_path, content=f"{prefix}- [ ] {body}\n".encode(), headers={"Content-Type": "text/markdown"})
        if response.is_error:
            raise IntegrationError(f"Obsidian ответил {response.status_code}")
        return PushResult(external_id=task_id(body))
