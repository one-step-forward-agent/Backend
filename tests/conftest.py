"""Integration tests run against a real PostgreSQL migrated with `alembic upgrade head` (see tests/README.md)."""

import dataclasses
import os
import uuid

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-1234567890")  # gitleaks:allow - test-only value
os.environ.setdefault("BOT_API_TOKEN", "test-bot-token-0123456789abcdef")  # gitleaks:allow - test-only value
os.environ.setdefault("SBER_AUTHORIZATION_KEY", "")
# The rule-based assistant is tested on its own; tests of the agent turn it on with a scripted model
os.environ.setdefault("ASSISTANT_AGENT", "false")
os.environ.setdefault("STORAGE_PATH", "/tmp/dayla-test-storage")

import httpx
import pytest

from app.core import ratelimit
from app.main import app

BOT_HEADERS = {"X-Bot-Token": os.environ["BOT_API_TOKEN"]}


@pytest.fixture(autouse=True)
def reset_rate_limits():
    ratelimit._hits.clear()
    yield


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def user(client):
    """A fresh user; returns (headers, chat_id) with Telegram already linked."""
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post("/auth/register", json={"email": email, "password": "password-123", "name": "Тест", "timezone": "Europe/Moscow"})
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    code = (await client.post("/api/telegram/link", headers=headers)).json()["code"]
    chat_id = uuid.uuid4().int % 10**12
    linked = await client.post("/internal/bot/link", json={"code": code, "chat_id": chat_id, "username": "tester"}, headers=BOT_HEADERS)
    assert linked.status_code == 200, linked.text
    return headers, chat_id


@pytest.fixture
def fake_gigachat(monkeypatch):
    """Route assistant calls to a canned GigaChat answer: fake_gigachat(events, answer=None)."""
    from app.core.config import settings
    from app.services import chat
    from services import gigachat

    replies = {}

    async def process_message(self, text, timezone="Europe/Moscow", context="", calendar=""):
        return {"events": replies["events"], "answer": replies.get("answer"), "intent": replies.get("intent")}

    async def chat_reply(self, *args, **kwargs):
        return "Ответ ассистента"

    monkeypatch.setattr(chat, "settings", dataclasses.replace(settings, gigachat_credentials="fake"))
    monkeypatch.setattr(gigachat.GigaChatClient, "process_message", process_message)
    monkeypatch.setattr(gigachat.GigaChatClient, "chat_reply", chat_reply)

    def configure(events, answer=None, intent=None):
        replies["events"] = events
        replies["answer"] = answer
        replies["intent"] = intent

    return configure
