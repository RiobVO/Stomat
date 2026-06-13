"""Синхронизация записей с Google Calendar (reconciliation, идемпотентна).

Направления:
- БД → GCal (экспорт): записи бота. Изменённые находим сравнением
  time_range с gcal_synced_range — ноль чтений из Google.
- GCal → БД (импорт): ручные события клиники. Они — истина: блокируют
  слоты записями source='gcal_import'. Свои события (маркер navbat_id
  в extendedProperties) — наоборот: истина в БД, ручная правка
  откатывается с алертом админу.

Событие несёт услугу по-русски, имя и телефон пациента (решение владельца
11.06: он живёт в календаре, событие должно отвечать «кто и зачем») —
ПД уходит в Google-аккаунт клиники, зафиксировано в docs/PRIVACY.md.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from navbat.calendar.api import ResyncRequired
from navbat.crypto import decrypt_text
from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.dialog.replies import Button, Reply, service_label, t
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import SchedulingError
from navbat.telegram.worker import send_reply

log = logging.getLogger("navbat.calendar")

RELOCATION_SCAN_DAYS = 14  # горизонт поиска слота для вытесненной записи


class CalendarSync:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        api,  # GoogleCalendarAPI | FakeCalendarAPI
        notifier: EscalationNotifier | None = None,
        tg_api=None,  # TelegramAPI — уведомления пациентам о переносах
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._api = api
        self._notifier = notifier or LoggingEscalation()
        self._tg_api = tg_api

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
        moved = self._import(doctor_id, row.gcal_calendar_id, row.gcal_sync_token,
                             row.buffer_min)
        if moved:
            # конфликт-переносы породили новые/отменённые записи — доносим в GCal
            self._export(doctor_id, row.gcal_calendar_id)

    # ── Экспорт: БД → GCal ───────────────────────────────────────────────

    def _export(self, doctor_id: uuid.UUID, calendar_id: str) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            pending = session.execute(
                text("""
                    SELECT a.id, lower(a.time_range) AS start, upper(a.time_range) AS finish,
                           a.status, a.gcal_event_id,
                           s.name AS service,
                           p.name_encrypted, p.phone_encrypted
                    FROM appointment a
                    LEFT JOIN service s ON s.id = a.service_id
                    LEFT JOIN patient p ON p.id = a.patient_id
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
        # услуга по-русски + имя/телефон пациента (решение владельца 11.06);
        # NULL-поля (gcal_import, старые пациенты, /forget) — строки пропускаются
        service = (service_label(appointment.service, "ru")
                   if appointment.service else "Приём")
        name = (decrypt_text(appointment.name_encrypted)
                if appointment.name_encrypted else None)
        body = {
            "summary": (f"{service} — {name} (Navbat)" if name
                        else f"{service} (Navbat)"),
            "start": {"dateTime": _iso(appointment.start)},
            "end": {"dateTime": _iso(appointment.finish)},
            "extendedProperties": {"private": {"navbat_id": str(appointment.id)}},
        }
        if appointment.phone_encrypted:
            # нормализованный номер хранится без «+» — отдаём в E.164
            body["description"] = f"Телефон: +{decrypt_text(appointment.phone_encrypted)}"
        return body

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
                sync_token: str | None, buffer_min: int) -> bool:
        """GCal → БД. True — были конфликт-переносы (нужен повторный экспорт)."""
        try:
            events, next_token = self._api.list_events(calendar_id,
                                                       sync_token=sync_token)
        except ResyncRequired:
            log.warning("календарь %s: syncToken протух, full resync", calendar_id)
            events, next_token = self._api.list_events(calendar_id, sync_token=None)
        moved = False
        for event in events:
            if _own_marker(event):
                self._reconcile_own(calendar_id, event)
            else:
                moved = self._apply_manual(doctor_id, event, buffer_min) or moved
        if next_token:
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                session.execute(
                    text("UPDATE doctor SET gcal_sync_token = :token WHERE id = :id"),
                    {"token": next_token, "id": doctor_id},
                )
        return moved

    def _reconcile_own(self, calendar_id: str, event: dict) -> None:
        """Своё событие правили руками — истина в БД, откатываем с алертом."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(
                text("""
                    SELECT a.id, a.status, a.gcal_event_id, a.tg_chat_id,
                           lower(a.time_range) AS start, upper(a.time_range) AS finish,
                           s.name AS service,
                           p.name_encrypted, p.phone_encrypted
                    FROM appointment a
                    LEFT JOIN service s ON s.id = a.service_id
                    LEFT JOIN patient p ON p.id = a.patient_id
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

    def _apply_manual(self, doctor_id: uuid.UUID, event: dict,
                      buffer_min: int) -> bool:
        """Одно ручное событие → БД. True — случился конфликт-перенос."""
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
            return False

        span = _event_span(event, self._clinic_tz())
        if span is None:
            log.warning("событие %s без времени — пропущено", event.get("id"))
            return False
        all_day = "date" in event.get("start", {})
        manual_buffer = 0 if all_day else buffer_min
        if self._try_write_manual(existing, doctor_id, event["id"], span, manual_buffer):
            return False
        # ручное легло поверх записи бота — приоритет у ручного (BRIEF):
        # двигаем записи бота, потом вставляем блок повторно
        moved = self._resolve_conflict(doctor_id, span, manual_buffer)
        if not self._try_write_manual(existing, doctor_id, event["id"], span,
                                      manual_buffer):
            # живой hold: не трогаем (пациент выбирает прямо сейчас),
            # hold истечёт за 3 минуты — заберём блок следующим циклом
            log.warning("событие %s: конфликт не разрешён (живой hold?) — "
                        "повтор следующим циклом", event["id"])
        return moved

    def _try_write_manual(self, existing, doctor_id: uuid.UUID, event_id: str,
                          span: tuple[datetime, datetime], buffer_min: int) -> bool:
        start, finish = span
        try:
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                # протухшие hold физически блокируют exclusion — экспирим
                session.execute(
                    text("UPDATE appointment SET status = 'expired' "
                         "WHERE doctor_id = :doctor AND status = 'hold' "
                         "AND hold_expires_at <= now()"),
                    {"doctor": doctor_id},
                )
                if existing:
                    if (existing.start, existing.finish) == span:
                        return True
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
                         "buffer": buffer_min, "event": event_id},
                    )
                session.flush()
            return True
        except IntegrityError:
            return False

    # ── Conflict-resolution: приоритет ручного ───────────────────────────

    def _resolve_conflict(self, doctor_id: uuid.UUID,
                          span: tuple[datetime, datetime],
                          manual_buffer: int) -> bool:
        victims = self._find_victims(doctor_id, span, manual_buffer)
        booked = [v for v in victims if v.status == "booked"]
        if not booked:
            return False
        scheduler = SchedulingEngine(self._session_factory, self._clinic_id,
                                     actor="calendar_sync")
        for victim in booked:
            self._relocate(scheduler, doctor_id, victim, span, manual_buffer)
        return True

    def _find_victims(self, doctor_id: uuid.UUID,
                      span: tuple[datetime, datetime], manual_buffer: int):
        """Записи бота, пересекающиеся с ручным блоком (буферы обеих сторон)."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return session.execute(
                text("""
                    SELECT a.id, a.status, a.patient_id, a.tg_chat_id, a.service_id,
                           a.buffer_min, lower(a.time_range) AS start,
                           s.name AS service
                    FROM appointment a LEFT JOIN service s ON s.id = a.service_id
                    WHERE a.doctor_id = :doctor AND a.source != 'gcal_import'
                      AND a.status IN ('hold', 'booked')
                      AND tstzrange(lower(a.time_range),
                                    upper(a.time_range)
                                    + (a.buffer_min * interval '1 minute'), '[)')
                          && tstzrange(:start,
                                       :finish + (:buffer * interval '1 minute'), '[)')
                """),
                {"doctor": doctor_id, "start": span[0], "finish": span[1],
                 "buffer": manual_buffer},
            ).all()

    def _relocate(self, scheduler: SchedulingEngine, doctor_id: uuid.UUID,
                  victim, span: tuple[datetime, datetime], manual_buffer: int) -> None:
        scheduler.cancel(victim.id)
        tz = self._clinic_tz()
        old_label = f"{victim.start.astimezone(tz):%d.%m %H:%M}"
        lang = self._chat_lang(victim.tg_chat_id)

        new_start, alternatives = self._relocation_slot(
            scheduler, doctor_id, victim, span, manual_buffer, tz)
        if new_start is None:
            self._notify_unrelocatable(victim, old_label, lang)
            return

        try:
            new_id = scheduler.hold(doctor_id, victim.service_id, new_start,
                                    patient_id=victim.patient_id,
                                    tg_chat_id=victim.tg_chat_id)
            scheduler.confirm(new_id)
        except SchedulingError:
            # слот перехвачен конкурентной бронью между выбором и hold (либо
            # confirm не прошёл). Жертва уже отменена — не теряем её молча:
            # деградируем в «перенести некуда» (пациент + админ уведомлены).
            log.warning("перенос записи %s не удался (слот перехвачен?) — "
                        "деградация в отмену", victim.id)
            self._notify_unrelocatable(victim, old_label, lang)
            return
        new_label = f"{new_start.astimezone(tz):%d.%m %H:%M}"
        if victim.tg_chat_id:
            # кнопки альтернатив работают через resched-поток FSM
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                conversation = load_conversation(session, victim.tg_chat_id)
                conversation.state = "resched_offer_slots"
                ctx = conversation.context
                ctx.resched_id = str(new_id)
                ctx.resched_doctor = str(doctor_id)
                ctx.service = victim.service
                ctx.date = str(new_start.astimezone(tz).date())
                ctx.lang = lang
                save_conversation(session, conversation)
            buttons = tuple(
                Button(f"{alt.astimezone(tz):%d.%m %H:%M}", f"reslot:{alt.isoformat()}")
                for alt in alternatives
            )
            self._notify_patient(victim.tg_chat_id,
                                 Reply(t("conflict_moved", lang,
                                         old=old_label, new=new_label), buttons))
        self._notifier.notify(victim.tg_chat_id or 0,
                              f"запись {old_label} вытеснена ручным событием — "
                              f"перенесена на {new_label}",
                              {"appointment": str(new_id)})

    def _notify_unrelocatable(self, victim, old_label: str, lang: str) -> None:
        """Жертву вытеснили, перенести некуда (нет слота ИЛИ перенос сорвался) —
        уведомляем пациента и админа об отмене, не теряем запись молча."""
        self._notify_patient(victim.tg_chat_id,
                             Reply(t("conflict_cancelled", lang, old=old_label)))
        self._notifier.notify(victim.tg_chat_id or 0,
                              f"запись {old_label} вытеснена ручным событием, "
                              f"перенести некуда — отменена",
                              {"appointment": str(victim.id)})

    def _relocation_slot(self, scheduler: SchedulingEngine, doctor_id: uuid.UUID,
                         victim, span: tuple[datetime, datetime],
                         manual_buffer: int, tz: ZoneInfo):
        """Ближайший слот, не задевающий ручной блок (он ещё не в БД)."""
        if victim.service_id is None:
            return None, []
        manual_lo = span[0]
        manual_hi = span[1] + timedelta(minutes=manual_buffer)
        victim_buffer = timedelta(minutes=victim.buffer_min)
        first_day = victim.start.astimezone(tz).date()
        for offset in range(RELOCATION_SCAN_DAYS + 1):
            day = first_day + timedelta(days=offset)
            slots = [
                slot for slot in scheduler.find_free_slots(doctor_id,
                                                           victim.service_id, day)
                if not (slot.start < manual_hi and manual_lo < slot.end + victim_buffer)
            ]
            if slots:
                return slots[0].start, [s.start for s in slots[1:5]]
        return None, []

    def _chat_lang(self, chat_id: int | None) -> str:
        if not chat_id:
            return "ru"
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            lang = session.execute(
                text("SELECT context ->> 'lang' FROM conversation "
                     "WHERE tg_chat_id = :chat"),
                {"chat": chat_id},
            ).scalar_one_or_none()
        return lang or "ru"

    def _notify_patient(self, chat_id: int | None, reply: Reply) -> None:
        if self._tg_api is None or not chat_id:
            log.warning("уведомление пациенту %s не доставлено (нет TG): %s",
                        chat_id, reply.text)
            return
        send_reply(self._tg_api, self._session_factory, self._clinic_id,
                   chat_id, reply)

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
