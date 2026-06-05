"""Финальная перепроверка слота в GCal перед confirm (BRIEF, «целостность записи»).

Закрывает окно: событие уже в календаре, но sync ещё не довёз его в БД.
Деградация мягкая — Google недоступен или календарь не привязан →
пропускаем: exclusion constraint в БД остаётся жёсткой гарантией.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.calendar.api import CalendarAPIError
from navbat.db.base import tenant_transaction

log = logging.getLogger("navbat.calendar")


class CalendarSlotGuard:
    def __init__(self, session_factory: sessionmaker[Session],
                 clinic_id: uuid.UUID, api) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._api = api

    def is_free(self, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            calendar_id = session.execute(
                text("SELECT gcal_calendar_id FROM doctor WHERE id = :id"),
                {"id": doctor_id},
            ).scalar_one_or_none()
        if not calendar_id:
            return True
        try:
            return not self._api.free_busy(calendar_id, start.isoformat(),
                                           end.isoformat())
        except CalendarAPIError as e:
            log.warning("freeBusy недоступен (%s) — перепроверка пропущена", e)
            return True
