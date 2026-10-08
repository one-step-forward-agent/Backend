"""Signing up and logging in through Google: the account comes from the provider, signing up also connects
the calendar, and an email that already has a Dayla account is never logged in by email alone."""

import uuid
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.api.deps import ACCESS_COOKIE
from app.main import app
from tests.test_integrations import TZ, oauth_connect, services, titles  # noqa: F401 - services is a fixture

ONBOARDING = "/onboarding/integrations"


@pytest.fixture
async def browser():
    """A browser without a Dayla session."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
def google_account(services):
    email = f"new-{uuid.uuid4().hex[:10]}@gmail.com"
    services.google_profile = {"sub": f"g-{uuid.uuid4().hex}", "email": email, "email_verified": True, "name": "Ольга"}
    return email


async def start(browser, mode: str, **body) -> dict:
    started = await browser.post(f"/auth/oauth/google/start", json={"mode": mode, **body})
    assert started.status_code == 200, started.text
    return parse_qs(urlparse(started.json()["authorization_url"]).query)


async def callback(browser, query: dict) -> httpx.Response:
    response = await browser.get("/auth/google/callback", params={"code": "code-1", "state": query["state"][0]})
    assert response.status_code == 303, response.text
    return response


def session_headers(response: httpx.Response) -> dict:
    cookies = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        cookies.load(header)
    assert ACCESS_COOKIE in cookies, "no session was started"
    return {"Authorization": f"Bearer {cookies[ACCESS_COOKIE].value}"}


def redirect(response: httpx.Response) -> tuple[str, dict]:
    target = urlparse(response.headers["location"])
    return target.path, {key: values[0] for key, values in parse_qs(target.query).items()}


async def test_sign_up_with_google_creates_the_account_and_connects_the_calendar(browser, services, google_account):
    query = await start(browser, "signup", consent=True, return_to=ONBOARDING, timezone="Asia/Yekaterinburg")
    assert "calendar" in query["scope"][0]
    response = await callback(browser, query)
    assert response.headers["location"] == f"{ONBOARDING}?connected=google"
    headers = session_headers(response)
    me = (await browser.get("/api/me", headers=headers)).json()
    assert me["email"] == google_account and me["name"] == "Ольга" and me["timezone"] == "Asia/Yekaterinburg"
    listed = {item["slug"]: item for item in (await browser.get("/api/integrations", headers=headers)).json()}
    assert listed["google"]["connection"]["status"] == "connected"
    assert await titles(browser, headers) == ["Стендап в Google"]

    # Later the same Google account logs in, asking only for the account
    query = await start(browser, "login", return_to="/app/today")
    assert query["scope"] == ["openid email profile"] and "access_type" not in query
    response = await callback(browser, query)
    assert response.headers["location"] == "/app/today"
    assert (await browser.get("/api/me", headers=session_headers(response))).json()["id"] == me["id"]

    # Signing up again with it logs into the same account instead of creating another one
    response = await callback(browser, await start(browser, "signup", consent=True, return_to=ONBOARDING))
    assert (await browser.get("/api/me", headers=session_headers(response))).json()["id"] == me["id"]


async def test_sign_up_needs_consent(browser, services):
    refused = await browser.post("/auth/oauth/google/start", json={"mode": "signup", "return_to": ONBOARDING})
    assert refused.status_code == 400 and "согласие" in refused.json()["detail"]
    assert (await browser.post("/auth/oauth/notion/start", json={"mode": "login"})).status_code == 404


async def test_existing_email_is_not_logged_in_by_google(browser, client, services, google_account):
    registered = await client.post("/auth/register", json={"email": google_account, "password": "password-123", "timezone": "Europe/Moscow"})
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    response = await callback(browser, await start(browser, "signup", consent=True, return_to=ONBOARDING))
    path, params = redirect(response)
    assert path == "/login" and params["next"] == ONBOARDING and "уже есть" in params["error"]
    assert ACCESS_COOKIE not in response.headers.get("set-cookie", "")
    response = await callback(browser, await start(browser, "login"))
    path, params = redirect(response)
    assert path == "/login" and "паролю" in params["error"]

    # Once the owner connects this Google account while logged in, it logs in too
    await oauth_connect(client, headers, "google")
    response = await callback(browser, await start(browser, "login"))
    assert response.headers["location"] == "/app"
    assert (await browser.get("/api/me", headers=session_headers(response))).json()["email"] == google_account


async def test_cancelled_login_returns_to_the_login_page(browser, services):
    query = await start(browser, "login", return_to="/app/today")
    cancelled = await browser.get("/auth/google/callback", params={"error": "access_denied", "state": query["state"][0]})
    path, params = redirect(cancelled)
    assert path == "/login" and params["error"] == "Подключение отменено."


async def test_login_without_an_account_leads_to_registration(browser, services, google_account):
    path, params = redirect(await callback(browser, await start(browser, "login")))
    assert path == "/register" and "ещё нет" in params["error"]


async def test_unverified_google_email_does_not_create_an_account(browser, services, google_account):
    services.google_profile["email_verified"] = False
    response = await callback(browser, await start(browser, "signup", consent=True, return_to=ONBOARDING))
    path, params = redirect(response)
    assert path == "/register" and "подтверждённый email" in params["error"]
    assert ACCESS_COOKIE not in response.headers.get("set-cookie", "")
