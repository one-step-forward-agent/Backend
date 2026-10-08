import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.services.integrations.base import EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem, guarded_client

DEFAULT_JQL = "assignee = currentUser() AND statusCategory != Done"
ATLASSIAN_API = "https://api.atlassian.com/ex/jira"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"


async def request_token(data: dict) -> dict:
    """A token request to Atlassian; an empty dict when it is refused or unreachable."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(ATLASSIAN_TOKEN_URL, json=data)
    except httpx.HTTPError:
        return {}
    return response.json() if not response.is_error else {}


class JiraIntegration(IntegrationProvider):
    """Jira Cloud through Atlassian OAuth 2.0 (3LO): requests go to api.atlassian.com/ex/jira/{cloud_id} with a
    Bearer token that lives an hour and is refreshed. Older connections with an email and API token still work."""

    slug = "jira"
    title = "Jira"
    description = "Вход через Atlassian: ваши задачи Jira со сроком (duedate) появляются в календаре."
    auth_type = "oauth"
    fields = []
    # The OAuth app asks only for read scopes, and there is no project to create issues in
    supports_push = False

    @property
    def site(self) -> str:
        return (self.config.get("site_url") or "").rstrip("/")

    @property
    def oauth(self) -> bool:
        # cloud_id is set only by the OAuth callback; an API token left from an older connection does not count then
        return bool(self.config.get("cloud_id")) or not self.secrets.get("api_token")

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Accept": "application/json"}
        try:
            if self.oauth:
                if not self.secrets.get("access_token") or not self.config.get("cloud_id"):
                    raise IntegrationError("Jira не подключена")
                headers["Authorization"] = f"Bearer {self.secrets['access_token']}"
                async with httpx.AsyncClient(timeout=20) as client:
                    return await client.request(method, f"{ATLASSIAN_API}/{self.config['cloud_id']}{path}", headers=headers, **kwargs)
            async with guarded_client(timeout=20, auth=(self.config["email"], self.secrets["api_token"])) as client:
                return await client.request(method, f"{self.site}{path}", headers=headers, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise IntegrationError("Jira недоступна") from error

    async def _refresh(self) -> bool:
        if not self.secrets.get("refresh_token") or not settings.jira_client_id or not settings.jira_client_secret:
            return False
        data = await request_token(
            {
                "grant_type": "refresh_token",
                "client_id": settings.jira_client_id,
                "client_secret": settings.jira_client_secret,
                "refresh_token": self.secrets["refresh_token"],
            }
        )
        if not data.get("access_token"):
            return False
        # Atlassian rotates refresh tokens: the new one must replace the old
        rotated = {"access_token": data["access_token"]}
        if data.get("refresh_token"):
            rotated["refresh_token"] = data["refresh_token"]
        self.secrets = {**self.secrets, **rotated}
        self.context.updated_secrets.update(rotated)
        return True

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        response = await self._send(method, path, **kwargs)
        if response.status_code == 401 and self.oauth and await self._refresh():
            response = await self._send(method, path, **kwargs)
        if response.status_code in (401, 403):
            raise IntegrationError("Atlassian отклонил доступ — подключите Jira заново" if self.oauth else "Jira отклонила email или API token")
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
        email = me.get("emailAddress") or self.config.get("email")
        return " · ".join(part for part in (me.get("displayName"), email, self.config.get("site_name")) if part) or "Jira"

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
                        url=f"{self.site}/browse/{issue['key']}" if self.site else None,
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
