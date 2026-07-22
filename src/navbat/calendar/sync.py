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
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from navbat.calendar.api import ResyncRequired
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
                text("SELECT gcal_calendar_id, gcal_sync_token, buffer_min "
                     "FROM doctor WHERE id = :id"),
                {"id": doctor_id},
            ).one_or_none()
        if row is None or not row.gcal_calendar_id:
            return  # врач без календаря не синхронизируется
        self._export(doctor_id, row.gcal_calendar_id)
        self._import(doctor_id, row.gcal_calendar_id, row.gcal_sync_token,
                     row.buffer_min)

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

    # ── Импорт: GCal → БД ────────────────────────────────────────────────

    def _import(self, doctor_id: uuid.UUID, calendar_id: str,
                sync_token: str | None, buffer_min: int) -> None:
        try:
            events, next_token = self._api.list_events(calendar_id,
                                                       sync_token=sync_token)
        except ResyncRequired:
            log.warning("календарь %s: syncToken протух, full resync", calendar_id)
            events, next_token = self._api.list_events(calendar_id, sync_token=None)
        for event in events:
            if _own_marker(event):
                self._reconcile_own(calendar_id, event)
            else:
                self._apply_manual(doctor_id, event, buffer_min)
        if next_token:
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                session.execute(
                    text("UPDATE doctor SET gcal_sync_token = :token WHERE id = :id"),
                    {"token": next_token, "id": doctor_id},
                )

    def _reconcile_own(self, calendar_id: str, event: dict) -> None:
        """Своё событие правили руками — истина в БД, откатываем с алертом."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(
                text("""
                    SELECT a.id, a.status, a.gcal_event_id, a.tg_chat_id,
                           lower(a.time_range) AS start, upper(a.time_range) AS finish,
                           s.name AS service
                    FROM appointment a LEFT JOIN service s ON s.id = a.service_id
                    WHERE a.id = CAST(:navbat_id AS uuid)
                """),
                {"navbat_id": _own_marker(event)},
            ).one_or_none()
        if row is None or row.status != "booked" or row.gcal_event_id != event["id"]:
            return  # эхо наших же delete/replace — не ручная правка
        if event.get("status") == "cancelled":
            recreated = self._api.insert_event(calendar_id, self._event_body(row))
            self._mark_synced(row.id, event_id=recreated["id"], synced=True)
            self._notifier.notify(row.tg_chat_id or 0,
                                  "событие записи удалили в календаре — восстановил; "
                                  "правки записей — через бота", {"appointment": str(row.id)})
            return
        span = _event_span(event, self._clinic_tz())
        if span and span != (row.start, row.finish):
            body = self._event_body(row)
            self._api.patch_event(calendar_id, event["id"],
                                  {"start": body["start"], "end": body["end"]})
            self._notifier.notify(row.tg_chat_id or 0,
                                  "событие записи сдвинули в календаре — вернул; "
                                  "переносы — через бота", {"appointment": str(row.id)})

    def _apply_manual(self, doctor_id: uuid.UUID, event: dict, buffer_min: int) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            existing = session.execute(
                text("SELECT id, status, lower(time_range) AS start, "
                     "upper(time_range) AS finish FROM appointment "
                     "WHERE gcal_event_id = :event AND source = 'gcal_import'"),
                {"event": event["id"]},
            ).one_or_none()

        if event.get("status") == "cancelled":
            if existing and existing.status == "booked":
                with tenant_transaction(self._session_factory, self._clinic_id) as session:
                    session.execute(
                        text("UPDATE appointment SET status = 'cancelled', "
                             "gcal_event_id = NULL WHERE id = :id"),
                        {"id": existing.id},
                    )
            return

        span = _event_span(event, self._clinic_tz())
        if span is None:
            log.warning("событие %s без времени — пропущено", event.get("id"))
            return
        start, finish = span
        all_day = "date" in event.get("start", {})
        try:
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                if existing:
                    if (existing.start, existing.finish) == span:
                        return
                    session.execute(
                        text("UPDATE appointment "
                             "SET time_range = tstzrange(:start, :finish, '[)') "
                             "WHERE id = :id"),
                        {"start": start, "finish": finish, "id": existing.id},
                    )
                else:
                    session.execute(
                        text("""
                            INSERT INTO appointment
                                (clinic_id, doctor_id, time_range, buffer_min,
                                 status, source, gcal_event_id)
                            VALUES (current_setting('app.clinic_id')::uuid, :doctor,
                                    tstzrange(:start, :finish, '[)'), :buffer,
                                    'booked', 'gcal_import', :event)
                        """),
                        {"doctor": doctor_id, "start": start, "finish": finish,
                         # all-day блок и так закрывает день — буфер не нужен
                         "buffer": 0 if all_day else buffer_min,
                         "event": event["id"]},
                    )
                session.flush()
        except IntegrityError:
            # ручное событие легло поверх записи бота — конфликт (приоритет
            # ручного); разрешение — следующий шаг инкремента
            self._resolve_conflict(doctor_id, event, span, buffer_min)

    def _resolve_conflict(self, doctor_id: uuid.UUID, event: dict,
                          span: tuple[datetime, datetime], buffer_min: int) -> None:
        log.error("конфликт ручного события %s с записью бота — разрешение "
                  "ещё не реализовано", event.get("id"))

    def _clinic_tz(self) -> ZoneInfo:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())


def _own_marker(event: dict) -> str | None:
    return event.get("extendedProperties", {}).get("private", {}).get("navbat_id")


def _event_span(event: dict, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """Интервал события в aware-datetime; all-day разворачивается в сутки TZ клиники."""
    start, end = event.get("start", {}), event.get("end", {})
    if "dateTime" in start and "dateTime" in end:
        return (datetime.fromisoformat(start["dateTime"]),
                datetime.fromisoformat(end["dateTime"]))
    if "date" in start and "date" in end:
        # у Google end.date эксклюзивен — это уже граница следующего дня
        lo = datetime.combine(datetime.fromisoformat(start["date"]).date(),
                              time(0, 0), tz)
        hi = datetime.combine(datetime.fromisoformat(end["date"]).date(),
                              time(0, 0), tz)
        return lo, hi
    return None


def _iso(moment: datetime) -> str:
    return moment.isoformat()
