from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.services.calendar_provider import CalendarProvider


class GoogleCalendarProvider(CalendarProvider):
    base_url = "https://www.googleapis.com/calendar/v3"

    def __init__(self, access_token: str, refresh_token: str | None = None):
        self.access_token = access_token
        self.refresh_token = refresh_token

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any] | None:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
            if response.status_code == 401 and self.refresh_token:
                token_response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": settings.google_client_id,
                        "client_secret": settings.google_client_secret,
                        "refresh_token": self.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                token_response.raise_for_status()
                self.access_token = token_response.json()["access_token"]
                headers["Authorization"] = f"Bearer {self.access_token}"
                response = await client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            return None if response.status_code == 204 else response.json()

    async def get_calendars(self) -> list[dict[str, Any]]:
        result = await self._request("GET", f"{self.base_url}/users/me/calendarList")
        return result.get("items", []) if result else []

    async def get_events(self, calendar_id: str, since: datetime | None = None) -> list[dict[str, Any]]:
        params = {"singleEvents": "false"}
        if since:
            params["timeMin"] = since.isoformat()
        result = await self._request("GET", f"{self.base_url}/calendars/{calendar_id}/events", params=params)
        return result.get("items", []) if result else []

    async def create_event(self, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"{self.base_url}/calendars/{calendar_id}/events", json=event) or {}

    async def update_event(self, calendar_id: str, event_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"{self.base_url}/calendars/{calendar_id}/events/{event_id}", json=event) or {}

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await self._request("DELETE", f"{self.base_url}/calendars/{calendar_id}/events/{event_id}")
