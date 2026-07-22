"""Синхронизация записей с Google Calendar (reconciliation, идемпотентна).

Направления:
- БД → GCal (экспорт): записи бота. Изменённые находим сравнением
  time_range с gcal_synced_range — ноль чтений из Google.
- GCal → БД (импорт): ручные события клиники. Они — истина: блокируют
  слоты записями source='gcal_import'. Свои события (маркер navbat_id
  в extendedProperties) — наоборот: истина в БД, ручная правка
  откатывается с алертом админу.

Имя пациента в событие не пишем — только услуга: GCal-событие не
должно расширять поверхность PII.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation

log = logging.getLogger("navbat.calendar")


class CalendarSync:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        api,  # GoogleCalendarAPI | FakeCalendarAPI
        notifier: EscalationNotifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._api = api
        self._notifier = notifier or LoggingEscalation()

    def sync_doctor(self, doctor_id: uuid.UUID) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(
                text("SELECT gcal_calendar_id FROM doctor WHERE id = :id"),
                {"id": doctor_id},
            ).one_or_none()
        if row is None or not row.gcal_calendar_id:
            return  # врач без календаря не синхронизируется
        self._export(doctor_id, row.gcal_calendar_id)

    # ── Экспорт: БД → GCal ───────────────────────────────────────────────

    def _export(self, doctor_id: uuid.UUID, calendar_id: str) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            pending = session.execute(
                text("""
                    SELECT a.id, lower(a.time_range) AS start, upper(a.time_range) AS finish,
                           a.status, a.gcal_event_id,
                           s.name AS service
                    FROM appointment a
                    LEFT JOIN service s ON s.id = a.service_id
                    WHERE a.doctor_id = :doctor AND a.source != 'gcal_import'
                      AND ((a.status = 'booked'
                            AND (a.gcal_event_id IS NULL
                                 OR a.gcal_synced_range IS DISTINCT FROM a.time_range))
                           OR (a.status IN ('cancelled', 'expired')
                               AND a.gcal_event_id IS NOT NULL))
                """),
                {"doctor": doctor_id},
            ).all()

        for appointment in pending:
            if appointment.status != "booked":
                self._api.delete_event(calendar_id, appointment.gcal_event_id)
                self._mark_synced(appointment.id, event_id=None, synced=False)
                continue
            body = self._event_body(appointment)
            if appointment.gcal_event_id is None:
                event = self._api.insert_event(calendar_id, body)
                self._mark_synced(appointment.id, event_id=event["id"], synced=True)
            else:
                self._api.patch_event(calendar_id, appointment.gcal_event_id,
                                      {"start": body["start"], "end": body["end"]})
                self._mark_synced(appointment.id,
                                  event_id=appointment.gcal_event_id, synced=True)

    @staticmethod
    def _event_body(appointment) -> dict:
        return {
            "summary": f"{appointment.service or 'Приём'} (Navbat)",
            "start": {"dateTime": _iso(appointment.start)},
            "end": {"dateTime": _iso(appointment.finish)},
            "extendedProperties": {"private": {"navbat_id": str(appointment.id)}},
        }

    def _mark_synced(self, appointment_id, event_id: str | None, synced: bool) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text("UPDATE appointment SET gcal_event_id = :event, "
                     "gcal_synced_range = CASE WHEN :synced THEN time_range END "
                     "WHERE id = :id"),
                {"event": event_id, "synced": synced, "id": appointment_id},
            )


def _iso(moment: datetime) -> str:
    return moment.isoformat()
