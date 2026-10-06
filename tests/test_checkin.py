from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, Notification, ReminderSettings, User
from app.services import insights, reminders, tasks
from tests.conftest import BOT_HEADERS


async def make_tasks(client, chat_id: int, texts: list[str]) -> list[int]:
    ids = []
    for text in texts:
        reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": text}, headers=BOT_HEADERS)).json()
        created = (await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)).json()
        ids += created["event_ids"]
    return ids


async def test_checkin_suggests_moving_untimed_tasks(client, user):
    headers, chat_id = user
    await client.put("/api/me/onboarding", json={"toneOfVoice": "strict", "workDays": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"], "workHoursTo": "23:59"}, headers=headers)
    await make_tasks(client, chat_id, ["разобрать почту сегодня", "написать отчёт сегодня в 23:00"])
    async with session_factory() as session:
        user_row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        tz = tasks.local_tz(user_row)
        now = datetime.now(tz).replace(second=0, microsecond=0)
        text, payload = await insights.checkin(session, user_row, now)
        assert "Контрольная точка дня" in text  # tone from onboarding
        assert "Разобрать почту" in text
        untimed = await session.scalar(select(Event).where(Event.user_id == user_row.id, Event.title == "Разобрать почту"))
        assert payload["event_ids"][0] == untimed.id
        assert payload["target"] > now.date().isoformat()

        # The outbox creates one check-in per day once its time has come
        settings_row = await session.get(ReminderSettings, user_row.id)
        settings_row.checkin_time = (now - timedelta(minutes=1)).time()
        await session.commit()
        claimed = [item for item in await reminders.claim(session, limit=200) if item["kind"] == "checkin" and item["chat_id"] == chat_id]
        assert len(claimed) == 1 and claimed[0]["payload"]["event_ids"] == payload["event_ids"]
        assert not [item for item in await reminders.claim(session, limit=200) if item["kind"] == "checkin" and item["chat_id"] == chat_id]

    url = f"/internal/bot/notifications/{claimed[0]['id']}/checkin"
    moved = (await client.post(url, json={"chat_id": chat_id, "action": "move"}, headers=BOT_HEADERS)).json()
    assert moved["moved"] == 1
    again = (await client.post(url, json={"chat_id": chat_id, "action": "move"}, headers=BOT_HEADERS)).json()
    assert again["moved"] == 0
    assert (await client.post(url, json={"chat_id": chat_id + 1, "action": "move"}, headers=BOT_HEADERS)).status_code == 404


async def test_no_checkin_when_day_is_done(client, user):
    _, chat_id = user
    [event_id] = await make_tasks(client, chat_id, ["полить цветы сегодня"])
    await client.post(f"/internal/bot/chat/{chat_id}/events/{event_id}/complete", json={"completed": True}, headers=BOT_HEADERS)
    async with session_factory() as session:
        user_row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        assert await insights.checkin(session, user_row, datetime.now(tasks.local_tz(user_row))) is None


async def test_series_extension(client, user):
    headers, chat_id = user
    await make_tasks(client, chat_id, ["каждый понедельник планёрка в 10:00"])
    async with session_factory() as session:
        user_row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        before = list(await session.scalars(select(Event).where(Event.user_id == user_row.id)))
        later = datetime.now(timezone.utc) + timedelta(days=80)
        assert await tasks.extend_series(session, later, force=True) > 0
        after = list(await session.scalars(select(Event).where(Event.user_id == user_row.id).order_by(Event.start_at)))
        assert len(after) > len(before)
        assert all(event.start_at.astimezone(tasks.local_tz(user_row)).time() == time(10, 0) for event in after)
        assert len({event.start_at for event in after}) == len(after)
        await session.execute(Notification.__table__.delete().where(Notification.user_id == user_row.id))
        await session.commit()
