"""Тонкий клиент Google Calendar API v3 (httpx, синхронный).

Зачем не google-api-python-client: discovery-магия, ~10 транзитивных
зависимостей и asyncio-несовместимый кэш ради четырёх REST-методов.
OAuth: access-токен живёт в памяти, 401 → refresh → один повтор;
провал refresh — CalendarAuthError (алерт: токены умирают у
неверифицированных приложений). 410 GONE — syncToken протух,
вызывающий обязан сделать full resync.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Sequence

import httpx

log = logging.getLogger("navbat.calendar")

TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE_URL = "https://www.googleapis.com/calendar/v3"
_RETRY_DELAYS = (1, 2, 4)


class CalendarAPIError(Exception):
    """Ошибка Calendar API (после повторов либо логическая)."""


class CalendarAuthError(CalendarAPIError):
    """Refresh-токен мёртв — нужна повторная авторизация клиники."""


class ResyncRequired(CalendarAPIError):
    """syncToken протух (410) — требуется full sync без токена."""


class GoogleCalendarAPI:
    def __init__(
        self,
        refresh_token: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        client: httpx.Client | None = None,
        retry_delays: Sequence[float] = _RETRY_DELAYS,
    ) -> None:
        self._refresh_token = refresh_token
        self._client_id = client_id or os.environ.get("NAVBAT_GCAL_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("NAVBAT_GCAL_CLIENT_SECRET", "")
        self._client = client or httpx.Client(timeout=httpx.Timeout(15, connect=5))
        self._retry_delays = tuple(retry_delays)
        self._access_token: str | None = None

    def check_auth(self) -> None:
        """Проверка живости refresh-токена (преддемо-чеклист). Бросает CalendarAuthError."""
        self._refresh_access_token()

    # ── Календарные методы ───────────────────────────────────────────────

    def list_events(self, calendar_id: str, sync_token: str | None = None,
                    time_min: str | None = None) -> tuple[list[dict], str | None]:
        """Все события (с пагинацией) + nextSyncToken для следующего раза."""
        params: dict = {"singleEvents": "true", "showDeleted": "true",
                        "maxResults": "250"}
        if sync_token:
            params["syncToken"] = sync_token
        elif time_min:
            params["timeMin"] = time_min
        events: list[dict] = []
        next_sync_token = None
        while True:
            page = self._call("GET", f"/calendars/{calendar_id}/events", params=params)
            events.extend(page.get("items", []))
            next_sync_token = page.get("nextSyncToken", next_sync_token)
            page_token = page.get("nextPageToken")
            if not page_token:
                return events, next_sync_token
            params["pageToken"] = page_token

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        return self._call("POST", f"/calendars/{calendar_id}/events", json=body)

    def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return self._call("PATCH", f"/calendars/{calendar_id}/events/{event_id}",
                          json=body)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        # 404/410 — уже удалено: идемпотентность важнее строгости
        self._call("DELETE", f"/calendars/{calendar_id}/events/{event_id}",
                   missing_ok=True)

    def free_busy(self, calendar_id: str, time_min: str, time_max: str) -> bool:
        """True — интервал занят (финальная перепроверка перед confirm)."""
        result = self._call("POST", "/freeBusy", json={
            "timeMin": time_min, "timeMax": time_max,
            "items": [{"id": calendar_id}],
        })
        return bool(result["calendars"][calendar_id].get("busy"))

    # ── OAuth и транспорт ────────────────────────────────────────────────

    def _refresh_access_token(self) -> None:
        response = self._client.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        })
        if response.status_code != 200:
            raise CalendarAuthError(
                f"refresh не удался ({response.status_code}): {response.text[:200]}")
        self._access_token = response.json()["access_token"]

    def _call(self, method: str, path: str, params: dict | None = None,
              json: dict | None = None, missing_ok: bool = False):
        if self._access_token is None:
            self._refresh_access_token()
        refreshed = False
        last_error: Exception | None = None
        for attempt in range(len(self._retry_delays) + 1):
            if attempt:
                time.sleep(self._retry_delays[attempt - 1])
            try:
                response = self._client.request(
                    method, BASE_URL + path, params=params, json=json,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
            except httpx.TransportError as e:
                last_error = e
                log.warning("gcal %s %s: сеть (попытка %d): %s",
                            method, path, attempt + 1, e)
                continue
            if response.status_code == 401 and not refreshed:
                # access-токен истёк — единожды обновляем и повторяем
                refreshed = True
                self._refresh_access_token()
                continue
            if response.status_code == 410:
                raise ResyncRequired(path)
            if response.status_code in (404,) and missing_ok:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                last_error = CalendarAPIError(
                    f"{response.status_code}: {response.text[:200]}")
                log.warning("gcal %s %s: %d (попытка %d)", method, path,
                            response.status_code, attempt + 1)
                continue
            if response.status_code >= 400:
                raise CalendarAPIError(
                    f"{method} {path}: {response.status_code} {response.text[:300]}")
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        raise CalendarAPIError(f"{method} {path}: повторы исчерпаны: {last_error}")
