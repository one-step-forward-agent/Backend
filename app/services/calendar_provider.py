from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class CalendarProvider(ABC):
    @abstractmethod
    async def get_calendars(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_events(self, calendar_id: str, since: datetime | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def create_event(self, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def update_event(self, calendar_id: str, event_id: str, event: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        raise NotImplementedError
