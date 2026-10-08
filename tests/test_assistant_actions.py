"""The assistant really deletes, completes and analyses — and never claims a change it did not make."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from tests.conftest import BOT_HEADERS

TZ = ZoneInfo("Europe/Moscow")


def today():
    return datetime.now(TZ).date()


async def chat(client, headers, text: str) -> dict:
    response = await client.post("/api/assistant/chat", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def add(client, headers, text: str) -> list[int]:
    reply = await chat(client, headers, text)
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    return created["event_ids"]


async def titles(client, headers) -> list[str]:
    start = datetime.combine(today() - timedelta(days=30), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 1000}, headers=headers)).json()
    return sorted(event["title"] for event in events)


async def test_delete_all_events_really_deletes_after_confirmation(client, user):
    headers, _ = user
    for text in ["купить хлеб завтра", "созвон с Олегом послезавтра в 10:00", "каждый вторник в 19:00 бассейн"]:
        await add(client, headers, text)
    before = await titles(client, headers)
    assert len(before) > 3  # the series has several occurrences

    reply = await chat(client, headers, "удали все события")
    assert reply["kind"] == "delete_proposal" and reply["count"] == len(before)
    assert "message_id" in reply
    assert await titles(client, headers) == before  # nothing is removed before confirmation

    done = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert done == {"kind": "deleted", "count": len(before), "text": f"Удалила {len(before)} задач."}
    assert await titles(client, headers) == []


async def test_delete_one_task_or_a_day(client, user):
    headers, _ = user
    await add(client, headers, "встреча с Олегом завтра в 12:00")
    await add(client, headers, "купить молоко завтра")
    await add(client, headers, "забрать посылку послезавтра")

    one = await chat(client, headers, "удали встречу с Олегом")
    assert one["count"] == 1 and "Встреча с Олегом" in one["title"]
    await client.post(f"/api/assistant/drafts/{one['draft_id']}/confirm", headers=headers)
    assert await titles(client, headers) == ["Забрать посылку", "Купить молоко"]

    day = await chat(client, headers, "удали задачи на послезавтра")
    assert day["count"] == 1
    await client.post(f"/api/assistant/drafts/{day['draft_id']}/confirm", headers=headers)
    assert await titles(client, headers) == ["Купить молоко"]

    assert (await chat(client, headers, "удали встречу с марсианами"))["kind"] == "not_found"
    # "убрать квартиру завтра" is a new task, not a deletion
    assert (await chat(client, headers, "убрать квартиру завтра"))["kind"] == "proposal"


async def test_model_cannot_claim_a_change(client, user, fake_gigachat):
    headers, _ = user
    await add(client, headers, "купить хлеб завтра")
    fake_gigachat([], answer="Готово, я удалила все события из календаря!")
    reply = await chat(client, headers, "сделай так, чтобы в календаре было пусто")
    assert reply["kind"] == "answer" and "удалила" not in reply["text"] and "ничего не меняла" in reply["text"]
    assert await titles(client, headers) == ["Купить хлеб"]

    # When the model recognises a deletion, the app shows the real confirmation
    fake_gigachat([], answer="Удалила", intent="delete")
    reply = await chat(client, headers, "мне больше не нужны все эти дела")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 1


async def test_complete_from_chat(client, user):
    headers, chat_id = user
    [report] = await add(client, headers, "написать отчёт сегодня")
    await add(client, headers, "полить цветы сегодня")
    done = await chat(client, headers, "я сделала отчёт")
    assert done["kind"] == "completed" and [event["id"] for event in done["events"]] == [report]
    assert (await client.get(f"/api/events/{report}", headers=headers)).json()["completed_at"]

    rest = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "отметь все задачи на сегодня выполненными"}, headers=BOT_HEADERS)).json()
    assert rest["kind"] == "completed" and [event["title"] for event in rest["events"]] == ["Полить цветы"]
    assert (await chat(client, headers, "отметь все задачи на сегодня выполненными"))["kind"] == "not_found"


async def test_analysis_offers_moves_to_confirm(client, user):
    headers, _ = user
    start = datetime.combine(today() - timedelta(days=2), time.min, TZ)
    body = {"title": "Разобрать архив", "start_at": start.isoformat(), "end_at": (start + timedelta(days=1)).isoformat(), "all_day": True}
    overdue = (await client.post("/api/events", json=body, headers=headers)).json()
    fixed = (await client.post("/api/events", json={**body, "title": "Сдать документы", "is_fixed": True}, headers=headers)).json()

    reply = await chat(client, headers, "проанализируй мою неделю")
    assert reply["kind"] == "proposal" and reply["answer"]
    assert [item["event_id"] for item in reply["events"]] == [overdue["id"]]  # the fixed task stays
    assert reply["events"][0]["date"] > today().isoformat()
    await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)
    moved = (await client.get(f"/api/events/{overdue['id']}", headers=headers)).json()
    assert datetime.fromisoformat(moved["start_at"]).astimezone(TZ).date() > today()
    assert (await client.get(f"/api/events/{fixed['id']}", headers=headers)).json()["start_at"] == fixed["start_at"]

    # Nothing to move: an analysis in words only
    calm = await chat(client, headers, "что можно перенести?")
    assert calm["kind"] == "answer" and calm["text"]


async def test_rating_answers(client, user):
    headers, chat_id = user
    reply = await chat(client, headers, "что у меня завтра?")
    message_id = reply["message_id"]
    url = f"/api/assistant/messages/{message_id}/rating"
    assert (await client.post(url, json={"value": -1}, headers=headers)).json() == {"id": message_id, "rating": -1}
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    assert [item["rating"] for item in history if item["id"] == message_id] == [-1]
    assert (await client.post(url, json={"value": 0}, headers=headers)).json()["rating"] is None
    assert (await client.post(url, json={"value": 5}, headers=headers)).status_code == 422
    # Only the author's own assistant answers can be rated
    user_message = next(item["id"] for item in history if item["role"] == "user")
    assert (await client.post(f"/api/assistant/messages/{user_message}/rating", json={"value": 1}, headers=headers)).status_code == 404
    bot = await client.post(f"/internal/bot/chat/{chat_id}/messages/{message_id}/rating", json={"value": 1}, headers=BOT_HEADERS)
    assert bot.json()["rating"] == 1
    assert (await client.post(f"/internal/bot/chat/{chat_id + 1}/messages/{message_id}/rating", json={"value": 1}, headers=BOT_HEADERS)).status_code == 404


async def test_gigachat_token_is_reused(monkeypatch):
    from services import gigachat

    calls = []

    async def new_token(self, session):
        calls.append(1)
        return {"access_token": "token", "expires_at": (datetime.now().timestamp() + 1800) * 1000}

    monkeypatch.setattr(gigachat.GigaChatClient, "_new_token", new_token)
    monkeypatch.setitem(gigachat._token_cache, "value", None)
    client = gigachat.GigaChatClient()
    assert await client._token(None) == await client._token(None) == "token"
    assert len(calls) == 1


async def test_quick_commands_are_the_same_on_the_site_and_in_telegram(client, user):
    headers, chat_id = user
    await add(client, headers, "полить цветы сегодня")
    expected = {
        "Сегодня": ("agenda", "today"),
        "/tomorrow": ("agenda", "tomorrow"),
        "📆 Неделя": ("agenda", "week"),
        "Выполнено": ("agenda", "today"),
        "статистика": ("stats", None),
        "Помощь": ("help", None),
        "советы": ("advice", None),
        "Напоминания": ("reminders", None),
    }
    for text, (kind, scope) in expected.items():
        web = await chat(client, headers, text)
        bot = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": text}, headers=BOT_HEADERS)).json()
        for reply in (web, bot):
            assert reply["kind"] == kind, (text, reply)
            assert reply.get("scope") == scope
        assert {key for key in web if key != "message_id"} == {key for key in bot if key != "message_id"}
    marking = await chat(client, headers, "отметить выполненные")
    assert marking["mark"] is True and marking["days"][0]["events"][0]["title"] == "Полить цветы"
    stats = await chat(client, headers, "/stats")
    assert stats["today"]["total"] == 1 and "streak" in stats and "best_streak" in stats
    reminders = await chat(client, headers, "напоминания")
    assert reminders["settings"]["evening_time"] == "21:00:00"


async def test_undo_from_the_web_chat_and_topic_from_telegram(client, user):
    headers, chat_id = user
    ids = await add(client, headers, "каждый четверг в 18:00 йога")
    assert (await client.post("/api/assistant/undo", json={"event_ids": ids}, headers=headers)).json() == {"deleted": len(ids)}
    assert await titles(client, headers) == []

    topic = (await client.post(f"/internal/bot/chat/{chat_id}/topic", json={"index": 0}, headers=BOT_HEADERS)).json()
    assert topic["kind"] == "topic" and topic["title"]
    assert (await client.post(f"/internal/bot/chat/{chat_id}/topic", json={"index": 4}, headers=BOT_HEADERS)).status_code == 404


async def test_web_agenda_matches_the_bot(client, user):
    headers, chat_id = user
    await add(client, headers, "полить цветы завтра")
    web = (await client.get("/api/assistant/agenda/tomorrow?mark=true", headers=headers)).json()
    bot = (await client.get(f"/internal/bot/chat/{chat_id}/agenda/tomorrow", headers=BOT_HEADERS)).json()
    assert web.pop("mark") is True and web == bot


async def test_explicit_add_creates_at_once_with_a_clean_title(client, user):
    headers, _ = user
    for text in ["добавь моделирование 2 кустов на завтра в 22 00", "добавь полив сада на завтра в 21-00", "запиши звонок маме на завтра 19.30"]:
        reply = await chat(client, headers, text)
        assert reply["kind"] == "created", (text, reply)
        [event] = reply["events"]
        assert event["title"] in ("Моделирование 2 кустов", "Полив сада", "Звонок маме"), event["title"]
        assert event["time"] in ("22:00", "21:00", "19:30")
        assert reply["event_ids"]  # the undo button needs them
    # Without "добавь" the task still waits for confirmation
    assert (await chat(client, headers, "купить хлеб завтра"))["kind"] == "proposal"


async def test_a_question_is_not_taken_for_the_awaited_time(client, user):
    headers, chat_id = user
    await chat(client, headers, "добавь моделирование 2 кустов на завтра в 22:00")
    draft = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "полить кусты завтра"}, headers=BOT_HEADERS)).json()
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{draft['draft_id']}/edit", json={"index": 0, "field": "time"}, headers=BOT_HEADERS)

    reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "что там с моделированием завтра?"}, headers=BOT_HEADERS)).json()
    assert reply["kind"] == "agenda", reply
    assert [event["title"] for day in reply["days"] for event in day["events"]] == ["Моделирование 2 кустов"]
    # A short value still edits the draft
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{draft['draft_id']}/edit", json={"index": 0, "field": "time"}, headers=BOT_HEADERS)
    edited = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "в 18:00"}, headers=BOT_HEADERS)).json()
    assert edited["kind"] == "proposal" and edited["events"][0]["time"] == "18:00"


async def test_a_new_request_closes_the_unconfirmed_draft(client, user):
    headers, _ = user
    first = await chat(client, headers, "купить хлеб завтра")
    second = await chat(client, headers, "забрать посылку послезавтра")
    assert (await client.post(f"/api/assistant/drafts/{first['draft_id']}/confirm", headers=headers)).status_code == 404
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    closed = [item["reply"] for item in history if item["reply"] and item["reply"].get("text") == "Черновик закрыт: вы отправили новый запрос."]
    assert len(closed) == 1
    assert (await client.post(f"/api/assistant/drafts/{second['draft_id']}/confirm", headers=headers)).json()["kind"] == "created"
