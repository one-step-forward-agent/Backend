import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_session
from app.models.models import Notification, User
from app.schemas import ReminderSettingsRead, ReminderSettingsUpdate, TelegramLinkResponse, TelegramStatus
from app.services import reminders

router = APIRouter(prefix="/api", tags=["reminders"])
_bot_username: str | None = None


async def bot_username() -> str | None:
    global _bot_username
    if settings.telegram_bot_username:
        return settings.telegram_bot_username.lstrip("@")
    if _bot_username is None and settings.telegram_bot_token:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe")
            _bot_username = response.json()["result"]["username"]
        except (httpx.HTTPError, KeyError, ValueError):
            return None
    return _bot_username


@router.get("/public/config")
async def public_config():
    return {"telegram_bot_username": await bot_username(), "app_url": settings.public_app_url or None}


@router.get("/reminders/settings", response_model=ReminderSettingsRead)
async def get_reminder_settings(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    reminder_settings = await reminders.get_settings(session, user)
    await session.commit()
    return reminder_settings


@router.put("/reminders/settings", response_model=ReminderSettingsRead)
async def update_reminder_settings(payload: ReminderSettingsUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    reminder_settings = await reminders.get_settings(session, user)
    reminders.apply_settings(reminder_settings, payload.model_dump(exclude_unset=True))
    await session.commit()
    await session.refresh(reminder_settings)
    return reminder_settings


@router.post("/reminders/test", status_code=status.HTTP_202_ACCEPTED)
async def send_test_reminder(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if not user.telegram_chat_id:
        raise HTTPException(status_code=409, detail="Сначала подключите Telegram")
    notification = await reminders.queue_test(session, user)
    return {"id": notification.id, "status": notification.status}


@router.get("/reminders/history")
async def reminder_history(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(20))
    return [
        {"id": item.id, "kind": item.kind, "status": item.status, "text": item.text, "scheduled_for": item.scheduled_for, "sent_at": item.sent_at, "error": item.error}
        for item in rows
    ]


@router.get("/telegram", response_model=TelegramStatus)
async def telegram_status(user: User = Depends(get_current_user)):
    return TelegramStatus(
        linked=user.telegram_chat_id is not None,
        username=user.telegram_username,
        linked_at=user.telegram_linked_at,
        bot_username=await bot_username(),
    )


@router.post("/telegram/link", response_model=TelegramLinkResponse)
async def telegram_link(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    code = reminders.issue_link_code(user)
    await session.commit()
    username = await bot_username()
    deep_link = f"https://t.me/{username}?start={code}" if username else None
    return TelegramLinkResponse(code=code, expires_at=user.telegram_link_expires_at, deep_link=deep_link)


@router.delete("/telegram", status_code=status.HTTP_204_NO_CONTENT)
async def telegram_unlink(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    user.telegram_chat_id = None
    user.telegram_username = None
    user.telegram_linked_at = None
    await session.commit()
