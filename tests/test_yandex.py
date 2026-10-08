"""Yandex Calendar: OAuth through Yandex ID, then CalDAV with the OAuth token — against a fake Yandex."""

import dataclasses
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.api import auth
from app.api.deps import ACCESS_COOKIE
from app.services.integrations import yandex

TZ = ZoneInfo("Europe/Moscow")

MULTI = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{}</d:multistatus>'


class FakeYandex:
    """OAuth endpoints of Yandex ID and a CalDAV server with one calendar."""

    def __init__(self):
        self.token = "token-1"
        self.profile = {"default_email": "olga@yandex.ru"}
        self.puts: list[tuple[str, str]] = []
        self.event_day = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y%m%d")

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://oauth.yandex.ru/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                assert form["refresh_token"] == ["refresh-1"]
                self.token = "token-2"
            return httpx.Response(200, json={"access_token": self.token, "refresh_token": "refresh-1", "expires_in": 31536000})
        if url.startswith("https://login.yandex.ru/info"):
            return httpx.Response(200, json=self.profile)
        if not url.startswith("https://caldav.yandex.ru/"):
            raise AssertionError(f"unexpected request {request.method} {url}")
        if request.headers.get("Authorization") != f"OAuth {self.token}":
            return httpx.Response(401)
        path = request.url.path
        if request.method == "PROPFIND" and path == "/":
            body = "<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/principals/users/olga/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/principals/users/olga/":
            body = "<d:response><d:href>/principals/users/olga/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/calendars/olga/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/calendars/olga/":
            body = (
                "<d:response><d:href>/calendars/olga/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
                "<d:response><d:href>/calendars/olga/events-1/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                "<d:displayname>Мои события</d:displayname></d:prop></d:propstat></d:response>"
            )
        elif request.method == "REPORT" and path == "/calendars/olga/events-1/":
            ical = (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:meet-1@yandex\r\nSUMMARY:Планёрка в Яндексе\r\n"
                f"DTSTART:{self.event_day}T070000Z\r\nDTEND:{self.event_day}T080000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
            )
            body = f"<d:response><d:href>/calendars/olga/events-1/meet-1.ics</d:href><d:propstat><d:prop><c:calendar-data>{ical}</c:calendar-data></d:prop></d:propstat></d:response>"
        elif request.method == "PUT" and path.startswith("/calendars/olga/events-1/"):
            self.puts.append((path, request.content.decode()))
            return httpx.Response(201)
        else:
            raise AssertionError(f"unexpected request {request.method} {url}")
        return httpx.Response(207, text=MULTI.format(body))


@pytest.fixture
def fake_yandex(monkeypatch):
    server = FakeYandex()
    transport = httpx.MockTransport(server.handle)
    configured = dataclasses.replace(auth.settings, yandex_client_id="client", yandex_client_secret="secret", yandex_redirect_uri="https://dayla.test/auth/yandex/callback")
    monkeypatch.setattr(auth, "settings", configured)
    monkeypatch.setattr(yandex, "settings", configured)
    monkeypatch.setattr(yandex, "guarded_client", lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))

    real_client = httpx.AsyncClient

    class ToFakeYandex(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    # Token exchange and profile requests in the OAuth callback, and token refresh
    monkeypatch.setattr(auth.httpx, "AsyncClient", ToFakeYandex)
    return server


async def connect(client, headers, fake_yandex) -> httpx.Response:
    started = await client.post("/api/integrations/yandex/connect", json={"values": {}, "return_to": "/onboarding/integrations"}, headers=headers)
    assert started.status_code == 200, started.text
    url = urlparse(started.json()["authorization_url"])
    query = parse_qs(url.query)
    assert url.netloc == "oauth.yandex.ru" and query["scope"] == ["calendar:all"]
    # Yandex sends the browser back with the Dayla session cookie
    client.cookies.set(ACCESS_COOKIE, headers["Authorization"].split()[1])
    return await client.get("/auth/yandex/callback", params={"code": "code-1", "state": query["state"][0]})


async def test_yandex_is_listed_as_oauth_calendar(client, user):
    headers, _ = user
    items = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    assert items["yandex"]["auth_type"] == "oauth" and items["yandex"]["supports_push"] and items["yandex"]["title"] == "Яндекс Календарь"


async def test_connect_imports_and_exports(client, user, fake_yandex):
    headers, _ = user
    callback = await connect(client, headers, fake_yandex)
    assert callback.status_code == 303 and callback.headers["location"] == "/onboarding/integrations?connected=yandex"

    # The first sync ran right after connecting
    start = datetime.combine(datetime.now(TZ).date(), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 100}, headers=headers)).json()
    assert [event["title"] for event in events] == ["Планёрка в Яндексе"]
    listed = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    assert listed["yandex"]["connection"]["status"] == "connected" and listed["yandex"]["connection"]["account_email"] == "olga@yandex.ru"

    # Sync again: the same event is updated, not duplicated
    synced = await client.post("/api/integrations/yandex/sync", headers=headers)
    assert synced.status_code == 200, synced.text
    assert synced.json()["updated"] == 1 and synced.json()["created"] == 0
    assert (await client.post("/api/integrations/yandex/test", headers=headers)).json() == {"status": "ok", "account": "olga@yandex.ru"}

    # A Dayla task goes to Yandex Calendar
    created = await client.post("/api/assistant/chat", json={"text": "купить хлеб послезавтра в 18:00"}, headers=headers)
    event_ids = (await client.post(f"/api/assistant/drafts/{created.json()['draft_id']}/confirm", headers=headers)).json()["event_ids"]
    exported = await client.post(f"/api/integrations/yandex/export/{event_ids[0]}", headers=headers)
    assert exported.status_code == 201, exported.text
    assert len(fake_yandex.puts) == 1 and "SUMMARY:Купить хлеб" in fake_yandex.puts[0][1]


async def test_connects_without_email(client, user, fake_yandex):
    # With the scope calendar:all alone Yandex ID returns neither email nor login
    headers, _ = user
    fake_yandex.profile = {"id": "1000", "client_id": "client"}
    callback = await connect(client, headers, fake_yandex)
    assert callback.status_code == 303, callback.text
    listed = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    assert listed["yandex"]["connection"]["status"] == "connected" and listed["yandex"]["connection"]["account_email"] is None
    assert (await client.post("/api/integrations/yandex/test", headers=headers)).json() == {"status": "ok", "account": "Яндекс Календарь"}


async def test_expired_token_is_refreshed(client, user, fake_yandex):
    headers, _ = user
    await connect(client, headers, fake_yandex)
    fake_yandex.token = "token-expired"  # Yandex no longer accepts token-1
    refreshed = []

    async def request_token(data):
        refreshed.append(data["grant_type"])
        fake_yandex.token = "token-2"
        return {"access_token": "token-2", "refresh_token": "refresh-2"}

    import app.services.integrations.yandex as module

    original = module.request_token
    module.request_token = request_token
    try:
        synced = await client.post("/api/integrations/yandex/sync", headers=headers)
    finally:
        module.request_token = original
    assert synced.status_code == 200, synced.text
    assert refreshed == ["refresh_token"]
    # The new token is stored: the next sync works without refreshing
    assert (await client.post("/api/integrations/yandex/sync", headers=headers)).status_code == 200
    assert refreshed == ["refresh_token"]
