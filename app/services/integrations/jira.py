import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.services.integrations.base import ConfigField, EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem, guarded_client

DEFAULT_JQL = "assignee = currentUser() AND statusCategory != Done"


class JiraIntegration(IntegrationProvider):
    slug = "jira"
    title = "Jira"
    description = "Jira Cloud REST API v3. Задачи со сроком (duedate) попадают в календарь; события можно отправить как задачи."
    fields = [
        ConfigField("site_url", "Адрес Jira", type="url", placeholder="https://your-team.atlassian.net"),
        ConfigField("email", "Email аккаунта Atlassian", placeholder="you@company.com"),
        ConfigField("api_token", "API token", type="password", secret=True, help="id.atlassian.com → Security → API tokens"),
        ConfigField("jql", "JQL фильтр", required=False, default=DEFAULT_JQL),
        ConfigField("project_key", "Проект для экспорта", required=False, placeholder="Например, FD"),
        ConfigField("issue_type", "Тип задачи для экспорта", required=False, default="Task"),
    ]

    @property
    def site(self) -> str:
        return self.config["site_url"].rstrip("/")

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with guarded_client(timeout=20, auth=(self.config["email"], self.secrets["api_token"])) as client:
                response = await client.request(method, f"{self.site}{path}", headers={"Accept": "application/json"}, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise IntegrationError("Jira недоступна") from error
        if response.status_code in (401, 403):
            raise IntegrationError("Jira отклонила email или API token")
        if response.is_error:
            messages = []
            try:
                body = response.json()
                messages = body.get("errorMessages", []) + list(body.get("errors", {}).values())
            except ValueError:
                pass
            raise IntegrationError("Jira: " + ("; ".join(messages) or f"ошибка {response.status_code}"))
        return response.json() if response.content else {}

    async def verify(self) -> str:
        me = await self._request("GET", "/rest/api/3/myself")
        return f"{me.get('displayName', '')} · {me.get('emailAddress') or self.config['email']}"

    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        jql = (self.config.get("jql") or DEFAULT_JQL).strip()
        base, *order = re.split(r"(?i)\border\s+by\b", jql, maxsplit=1)
        order_by = order[0].strip() if order else "duedate ASC"
        window = f'duedate >= "{start.date()}" AND duedate <= "{end.date()}"'
        query = f"({base.strip()}) AND {window}" if base.strip() else window
        query = f"{query} ORDER BY {order_by}"
        tz = ZoneInfo(self.context.timezone)
        items, token = [], None
        for _ in range(5):
            params = {"jql": query, "fields": "summary,duedate,status,priority", "maxResults": "100"}
            if token:
                params["nextPageToken"] = token
            data = await self._request("GET", "/rest/api/3/search/jql", params=params)
            for issue in data.get("issues", []):
                fields = issue.get("fields", {})
                if not fields.get("duedate"):
                    continue
                day = date.fromisoformat(fields["duedate"])
                status = (fields.get("status") or {}).get("name")
                items.append(
                    RemoteItem(
                        external_id=issue["key"],
                        title=f"[{issue['key']}] {fields.get('summary', '')}",
                        start_at=datetime.combine(day, time.min, tz),
                        end_at=datetime.combine(day + timedelta(days=1), time.min, tz),
                        all_day=True,
                        description=f"Статус: {status}" if status else None,
                        url=f"{self.site}/browse/{issue['key']}",
                    )
                )
            token = data.get("nextPageToken")
            if not token or data.get("isLast", True):
                break
        return items

    async def push_event(self, event: EventPayload) -> PushResult:
        project = (self.config.get("project_key") or "").strip()
        if not project:
            raise IntegrationError("Укажите проект для экспорта в настройках Jira")
        text = "\n".join(part for part in (event.description, event.location and f"Место: {event.location}") if part)
        fields = {
            "project": {"key": project},
            "summary": event.title,
            "issuetype": {"name": self.config.get("issue_type") or "Task"},
            "duedate": event.start_at.astimezone(ZoneInfo(event.timezone)).date().isoformat(),
        }
        if text:
            fields["description"] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
        issue = await self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        return PushResult(external_id=issue["key"], url=f"{self.site}/browse/{issue['key']}")
