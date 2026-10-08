"""Every integration end to end against fake services: connect (OAuth or credentials), the callback,
the first import, a repeated sync without duplicates, export and token refresh."""

import base64
import dataclasses
import json
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.api import auth
from app.api.deps import ACCESS_COOKIE
from app.services import google_calendar
from app.services.integrations import apple, jira, yandex

TZ = ZoneInfo("Europe/Moscow")
TOMORROW = datetime.now(TZ).date() + timedelta(days=1)
MULTI = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{}</d:multistatus>'


class FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.created: dict[str, list] = {"google": [], "notion": [], "apple": []}
        self.atlassian_profile = True
        self.google_profile = {"sub": "g-olga", "email": "olga@gmail.com", "email_verified": True, "name": "Ольга"}
        self.jira_token = "j-1"

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url).split("?")[0], request.method
        self.calls.append((method, url))
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        # Google
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "g-1", "refresh_token": "g-r", "expires_in": 3600})
        if url == "https://openidconnect.googleapis.com/v1/userinfo":
            return httpx.Response(200, json=self.google_profile)
        if url == "https://www.googleapis.com/calendar/v3/users/me/calendarList":
            return httpx.Response(200, json={"items": [{"id": "olga@gmail.com", "primary": True}]})
        if url == "https://www.googleapis.com/calendar/v3/calendars/primary/events":
            if method == "POST":
                self.created["google"].append(body["summary"])
                return httpx.Response(200, json={"id": f"g-new-{len(self.created['google'])}", "htmlLink": "https://calendar.google.com/x"})
            start = datetime.combine(TOMORROW, time(10), TZ)
            return httpx.Response(200, json={"items": [{"id": "g-ev-1", "summary": "Стендап в Google", "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(hours=1)).isoformat()}}]})
        # Notion
        if url == "https://api.notion.com/v1/oauth/token":
            assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"n-client:n-secret").decode()
            return httpx.Response(200, json={"access_token": "n-1", "bot_id": "bot-1", "workspace_name": "Olga WS", "owner": {"type": "user", "user": {"person": {"email": "olga@notion.so"}}}})
        if url.startswith("https://api.notion.com/"):
            assert request.headers["Authorization"] == "Bearer n-1"
            if url.endswith("/v1/users/me"):
                return httpx.Response(200, json={"id": "bot-1", "type": "bot", "bot": {"workspace_name": "Olga WS"}})
            if url.endswith("/v1/search"):
                return httpx.Response(200, json={"results": [
                    {"id": "db-notes", "title": [{"plain_text": "Заметки"}], "properties": {"Name": {"type": "title"}}},
                    {"id": "db-tasks", "title": [{"plain_text": "Задачи"}], "properties": {"Задача": {"type": "title"}, "Создано": {"type": "created_time"}, "Срок": {"type": "date"}}},
                ]})
            if url.endswith("/v1/databases/db-tasks/query"):
                assert body["filter"]["and"][0]["property"] == "Срок"
                return httpx.Response(200, json={"has_more": False, "results": [
                    {"id": "page-1", "url": "https://notion.so/page-1", "properties": {"Задача": {"type": "title", "title": [{"plain_text": "Сдать отчёт"}]}, "Срок": {"type": "date", "date": {"start": TOMORROW.isoformat()}}}},
                ]})
            if url.endswith("/v1/pages"):
                assert body["parent"] == {"database_id": "db-tasks"} and "Срок" in body["properties"]
                self.created["notion"].append(body["properties"]["Задача"]["title"][0]["text"]["content"])
                return httpx.Response(200, json={"id": "page-new", "url": "https://notion.so/page-new"})
        # Atlassian / Jira
        if url == "https://auth.atlassian.com/oauth/token":
            if body["grant_type"] == "refresh_token":
                assert body["refresh_token"] == "j-r1"
                self.jira_token = "j-2"
                return httpx.Response(200, json={"access_token": "j-2", "refresh_token": "j-r2", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "j-1", "refresh_token": "j-r1", "expires_in": 3600})
        if url == "https://api.atlassian.com/oauth/token/accessible-resources":
            return httpx.Response(200, json=[{"id": "cloud-1", "url": "https://olga.atlassian.net", "name": "Olga"}])
        if url == "https://api.atlassian.com/me":
            return httpx.Response(200, json={"email": "olga@corp.com"}) if self.atlassian_profile else httpx.Response(403)
        if url.startswith("https://api.atlassian.com/ex/jira/cloud-1/"):
            if request.headers["Authorization"] != f"Bearer {self.jira_token}":
                return httpx.Response(401, json={"message": "expired"})
            if url.endswith("/rest/api/3/myself"):
                return httpx.Response(200, json={"displayName": "Olga", "emailAddress": "olga@corp.com"})
            if url.endswith("/rest/api/3/search/jql"):
                return httpx.Response(200, json={"isLast": True, "issues": [{"key": "FD-7", "fields": {"summary": "Починить вход", "duedate": TOMORROW.isoformat(), "status": {"name": "В работе"}}}]})
        # iCloud CalDAV
        if url.startswith("https://caldav.icloud.com/"):
            if request.headers.get("Authorization") != "Basic " + base64.b64encode(b"olga@icloud.com:app-pass").decode():
                return httpx.Response(401)
            return self.caldav(request)
        raise AssertionError(f"unexpected request {method} {url}")

    def caldav(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PROPFIND" and path == "/":
            body = "<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/1/principal/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/1/principal/":
            body = "<d:response><d:href>/1/principal/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/1/calendars/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/1/calendars/":
            body = "<d:response><d:href>/1/calendars/home/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Дом</d:displayname></d:prop></d:propstat></d:response>"
        elif request.method == "REPORT":
            day = TOMORROW.strftime("%Y%m%d")
            ical = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:ic-1\r\nSUMMARY:Ужин с семьёй\r\nDTSTART;VALUE=DATE:{day}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
            body = f"<d:response><d:href>/1/calendars/home/ic-1.ics</d:href><d:propstat><d:prop><c:calendar-data>{ical}</c:calendar-data></d:prop></d:propstat></d:response>"
        elif request.method == "PUT":
            self.created["apple"].append(request.content.decode())
            return httpx.Response(201)
        else:
            raise AssertionError(f"unexpected CalDAV request {request.method} {path}")
        return httpx.Response(207, text=MULTI.format(body))


@pytest.fixture
def services(monkeypatch):
    fake = FakeServices()
    transport = httpx.MockTransport(fake.handle)
    configured = dataclasses.replace(
        auth.settings,
        google_client_id="g-client", google_client_secret="g-secret", google_redirect_uri="https://dayla.test/auth/google/callback",
        notion_client_id="n-client", notion_client_secret="n-secret", notion_redirect_uri="https://dayla.test/auth/notion/callback",
        jira_client_id="j-client", jira_client_secret="j-secret", jira_redirect_uri="https://dayla.test/auth/jira/callback",
        yandex_client_id="y-client", yandex_client_secret="y-secret", yandex_redirect_uri="https://dayla.test/auth/yandex/callback",
    )
    for module in (auth, google_calendar, jira, yandex):
        monkeypatch.setattr(module, "settings", configured)
    real_client = httpx.AsyncClient

    class ToFakes(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", ToFakes)
    for module in (apple, jira, yandex):
        monkeypatch.setattr(module, "guarded_client", lambda **kwargs: ToFakes(**kwargs))
    return fake


async def oauth_connect(client, headers, slug: str) -> httpx.Response:
    started = await client.post(f"/api/integrations/{slug}/connect", json={"values": {}, "return_to": "/app/integrations"}, headers=headers)
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    client.cookies.set(ACCESS_COOKIE, headers["Authorization"].split()[1])
    callback = await client.get(f"/auth/{slug}/callback", params={"code": "code-1", "state": query["state"][0]})
    assert callback.status_code == 303 and callback.headers["location"] == f"/app/integrations?connected={slug}", callback.text
    return started


async def titles(client, headers) -> list[str]:
    start = datetime.combine(datetime.now(TZ).date() - timedelta(days=1), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 200}, headers=headers)).json()
    return sorted(event["title"] for event in events)


async def connection(client, headers, slug: str) -> dict:
    items = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    return items[slug]["connection"]


async def test_every_integration_of_the_app_starts_its_sign_in(client, user, services):
    headers, _ = user
    hosts = {"google": "accounts.google.com", "yandex": "oauth.yandex.ru", "notion": "api.notion.com", "jira": "auth.atlassian.com"}
    listed = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    for slug, host in hosts.items():
        assert listed[slug]["auth_type"] == "oauth", slug
        response = await client.post(f"/api/integrations/{slug}/connect", json={"values": {}}, headers=headers)
        assert response.status_code == 200, (slug, response.text)
        assert urlparse(response.json()["authorization_url"]).netloc == host
    assert listed["apple"]["auth_type"] == "credentials"


async def test_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    assert await titles(client, headers) == ["Стендап в Google"]
    assert (await connection(client, headers, "google"))["status"] == "connected"
    synced = (await client.post("/api/integrations/google/sync", headers=headers)).json()
    assert synced["created"] == 0 and await titles(client, headers) == ["Стендап в Google"]
    # A task created in Dayla goes to Google Calendar by itself
    reply = (await client.post("/api/assistant/chat", json={"text": "купить хлеб послезавтра в 18:00"}, headers=headers)).json()
    await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)
    assert services.created["google"] == ["Купить хлеб"]


async def test_notion(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "notion")
    assert await titles(client, headers) == ["Сдать отчёт"]
    assert (await connection(client, headers, "notion"))["account_email"] == "olga@notion.so"
    assert (await client.post("/api/integrations/notion/test", headers=headers)).json() == {"status": "ok", "account": "База «Задачи»"}
    assert (await client.post("/api/integrations/notion/sync", headers=headers)).json()["created"] == 0
    reply = (await client.post("/api/assistant/chat", json={"text": "позвонить маме послезавтра"}, headers=headers)).json()
    event_id = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()["event_ids"][0]
    exported = await client.post(f"/api/integrations/notion/export/{event_id}", headers=headers)
    assert exported.status_code == 201, exported.text
    assert services.created["notion"] == ["Позвонить маме"]


async def test_jira_with_token_refresh(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "jira")
    assert await titles(client, headers) == ["[FD-7] Починить вход"]
    # The hour-long access token expires: the refresh token gets a new one, and the rotated pair is stored
    services.jira_token = "j-expired"
    synced = await client.post("/api/integrations/jira/sync", headers=headers)
    assert synced.status_code == 200, synced.text
    assert services.jira_token == "j-2"
    assert (await client.post("/api/integrations/jira/test", headers=headers)).json()["account"] == "Olga · olga@corp.com · Olga"
    assert await titles(client, headers) == ["[FD-7] Починить вход"]


async def test_jira_without_profile(client, user, services):
    # The profile only labels the account: the site name does when Atlassian refuses it
    headers, _ = user
    services.atlassian_profile = False
    await oauth_connect(client, headers, "jira")
    assert (await connection(client, headers, "jira"))["account_email"] == "Olga (cloud-1)"
    assert await titles(client, headers) == ["[FD-7] Починить вход"]


async def test_apple_with_app_password(client, user, services):
    headers, _ = user
    wrong = await client.post("/api/integrations/apple/connect", json={"values": {"username": "olga@icloud.com", "app_password": "wrong"}}, headers=headers)
    assert wrong.status_code == 400 and "пароль приложения" in wrong.json()["detail"]
    connected = await client.post("/api/integrations/apple/connect", json={"values": {"username": "olga@icloud.com", "app_password": "app-pass"}}, headers=headers)
    assert connected.status_code == 200, connected.text
    # The first sync ran right after connecting
    assert await titles(client, headers) == ["Ужин с семьёй"]
    assert (await client.post("/api/integrations/apple/sync", headers=headers)).json()["created"] == 0
    reply = (await client.post("/api/assistant/chat", json={"text": "позвонить маме послезавтра"}, headers=headers)).json()
    event_id = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()["event_ids"][0]
    assert (await client.post(f"/api/integrations/apple/export/{event_id}", headers=headers)).status_code == 201
    assert "SUMMARY:Позвонить маме" in services.created["apple"][0]


async def test_google_meeting_shared_by_two_users(client, user, services):
    """Both invitees of one meeting get it: Google gives it the same event id in both calendars."""
    import uuid

    headers, _ = user
    await oauth_connect(client, headers, "google")
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    registered = await client.post("/auth/register", json={"email": email, "password": "password-123", "name": "Второй", "timezone": "Europe/Moscow"})
    other = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    await oauth_connect(client, other, "google")
    assert await titles(client, headers) == ["Стендап в Google"]
    assert await titles(client, other) == ["Стендап в Google"]
