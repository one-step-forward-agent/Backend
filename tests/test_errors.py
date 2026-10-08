"""Users see plain messages: anything technical is "Ошибка сервера", details stay in the log."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.errors import SERVER_ERROR, plain
from app.main import app
from tests.conftest import BOT_HEADERS


@pytest.mark.parametrize(
    "detail, shown",
    [
        ("Неверный email или пароль", True),
        ("Notion отклонил доступ — подключите Notion заново", True),
        ("Неверный Apple ID или пароль приложения", True),
        ("Поле «Адрес Jira» обязательно", True),
        ("В базе нет свойства-даты «Due date»", True),
        ("Слишком много попыток входа, попробуйте через 15 минут", True),
        ("Event not found", False),
        ("Google OAuth failed: access_denied", False),
        ("CalDAV сервер ответил 503", False),
        ("CalDAV сервер не вернул principal", False),
        ("Notion: Could not find property", False),
        ("OAuth для jira не настроен", False),
        ("Адрес 10.0.0.1 указывает во внутреннюю сеть", False),
        (None, False),
    ],
)
def test_plain(detail, shown):
    assert (plain(detail) is not None) is shown


async def test_technical_details_are_hidden_plain_ones_kept(client, user):
    headers, _ = user
    missing = await client.get("/api/events/999999999", headers=headers)
    assert missing.status_code == 404 and missing.json() == {"detail": SERVER_ERROR}
    wrong = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"})
    assert wrong.json()["detail"] == "Неверный email или пароль"
    short = await client.post("/auth/register", json={"email": "short@example.com", "password": "123"})
    assert short.status_code == 422 and short.json() == {"detail": "Пароль должен быть не короче 8 символов."}


async def test_unexpected_error_is_a_server_error(user, monkeypatch):
    from app.services import chat

    headers, _ = user

    async def broken(*args, **kwargs):
        raise KeyError("draft_id")

    monkeypatch.setattr(chat, "handle_message", broken)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/api/assistant/chat", json={"text": "привет"}, headers=headers)
    assert response.status_code == 500 and response.json() == {"detail": SERVER_ERROR}


async def test_failed_oauth_returns_to_the_site(client, user):
    headers, _ = user
    broken = await client.get("/auth/google/callback", params={"code": "x", "state": "forged"})
    assert broken.status_code == 303
    target = urlparse(broken.headers["location"])
    assert target.path == "/app/integrations" and parse_qs(target.query)["error"] == ["Не удалось подключить сервис. Попробуйте ещё раз."]
    cancelled = await client.get("/auth/yandex/callback", params={"error": "access_denied"})
    assert parse_qs(urlparse(cancelled.headers["location"]).query)["error"] == ["Подключение отменено."]


async def test_bot_api_keeps_its_details(client):
    response = await client.get("/internal/bot/users/1", headers=BOT_HEADERS)
    assert response.status_code == 404 and response.json() == {"detail": "Telegram chat is not linked"}
