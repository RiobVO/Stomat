"""Push-каналы Google Calendar (events.watch) per врач-календарь (C-6).

Google канал не продлевает — открываем новый заранее (RENEW_LEAD до
expiration) и останавливаем старый. channel_id (uuid4) служит и токеном
в URL push-уведомлений: /gcal/push/<channel_id>, валидация — по полю
doctor.gcal_channel_id. Сбой watch (нет HTTPS, квота, неверифицированное
приложение) — warning: периодический поллинг продолжает работать.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from navbat.calendar.api import CalendarAPIError
from navbat.db.base import tenant_transaction

log = logging.getLogger("navbat.calendar.watch")

RENEW_LEAD = timedelta(hours=12)  # новый канал открываем заранее до истечения
PUSH_PATH_PREFIX = "/gcal/push/"


class GcalWatchManager:
    def __init__(self, session_factory, clinic_id, api, public_base_url,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._api = api
        self._base = public_base_url.rstrip("/")
        self._clock = clock

    def ensure_channels(self) -> None:
        """Открыть/продлить каналы всем врачам с календарём. Не бросает."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            rows = session.execute(text(
                "SELECT id, gcal_calendar_id, gcal_channel_id, gcal_resource_id, "
                "gcal_channel_expires_at FROM doctor "
                "WHERE gcal_calendar_id IS NOT NULL")).all()
        now = self._clock()
        for row in rows:
            if (row.gcal_channel_id and row.gcal_channel_expires_at is not None
                    and row.gcal_channel_expires_at - now > RENEW_LEAD):
                continue
            self._open_channel(row)

    def _open_channel(self, row) -> None:
        channel_id = str(uuid.uuid4())
        address = f"{self._base}{PUSH_PATH_PREFIX}{channel_id}"
        try:
            result = self._api.watch_events(row.gcal_calendar_id, channel_id,
                                            address)
        except CalendarAPIError as e:
            log.warning("watch %s не открыт (поллинг прикрывает): %s",
                        row.gcal_calendar_id, e)
            return
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(text(
                "UPDATE doctor SET gcal_channel_id = :channel, "
                "gcal_resource_id = :resource, gcal_channel_expires_at = :expires "
                "WHERE id = :id"),
                {"channel": channel_id, "resource": result.get("resourceId"),
                 "expires": _expiration(result), "id": row.id})
        log.info("watch-канал %s открыт для %s", channel_id, row.gcal_calendar_id)
        if row.gcal_channel_id and row.gcal_resource_id:
            try:
                self._api.stop_channel(row.gcal_channel_id, row.gcal_resource_id)
            except CalendarAPIError as e:
                log.warning("старый канал %s не остановлен: %s",
                            row.gcal_channel_id, e)


def _expiration(result: dict) -> datetime | None:
    raw = result.get("expiration")  # миллисекунды epoch, строкой
    if not raw:
        return None
    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
