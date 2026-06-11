"""Экспорт записей бота в Google Calendar: insert/patch/delete, идемпотентность.

GCal мокается FakeCalendarAPI (используется и в импорт/конфликт-тестах).
"""
from __future__ import annotations

import itertools

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.calendar.sync import CalendarSync
from navbat.db.base import tenant_transaction
from navbat.dialog.patients import create_patient
from navbat.scheduling.engine import SchedulingEngine

CAL = "doc-cal"


class FakeCalendarAPI:
    """Память вместо Google: события с семантикой showDeleted (cancelled).

    list_events отдаёт всё содержимое (full sync) — импорт обязан быть
    идемпотентным, поэтому инкрементальность фейку не нужна.
    """

    def __init__(self) -> None:
        self.calendars: dict[str, dict[str, dict]] = {}
        self._ids = itertools.count(1)
        self.insert_calls = 0
        self.patch_calls = 0
        self.list_tokens: list[str | None] = []  # какие syncToken передавали

    def events(self, calendar_id: str = CAL) -> dict[str, dict]:
        return self.calendars.setdefault(calendar_id, {})

    def seed_manual_event(self, start=None, end=None, day=None,
                          calendar_id: str = CAL, summary: str = "Ручная запись") -> str:
        """Событие, созданное админом руками (без navbat_id)."""
        event_id = f"manual{next(self._ids)}"
        if day is not None:  # all-day: date без dateTime, end эксклюзивен
            from datetime import timedelta
            times = {"start": {"date": day.isoformat()},
                     "end": {"date": (day + timedelta(days=1)).isoformat()}}
        else:
            times = {"start": {"dateTime": start.isoformat()},
                     "end": {"dateTime": end.isoformat()}}
        self.events(calendar_id)[event_id] = {
            "id": event_id, "status": "confirmed", "summary": summary, **times}
        return event_id

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        self.insert_calls += 1
        event = {**body, "id": f"ev{next(self._ids)}", "status": "confirmed"}
        self.events(calendar_id)[event["id"]] = event
        return event

    def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        self.patch_calls += 1
        self.events(calendar_id)[event_id].update(body)
        return self.events(calendar_id)[event_id]

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        event = self.events(calendar_id).get(event_id)
        if event:
            event["status"] = "cancelled"

    def list_events(self, calendar_id: str, sync_token=None, time_min=None):
        self.list_tokens.append(sync_token)
        return list(self.events(calendar_id).values()), f"SYNC{len(self.list_tokens)}"

    def free_busy(self, calendar_id: str, time_min: str, time_max: str) -> bool:
        return False


def bind_calendar(admin_engine, doctor_id, calendar_id=CAL) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET gcal_calendar_id = :cal WHERE id = :id"),
                     {"cal": calendar_id, "id": doctor_id})


def make_sync(app_session_factory, clinic_id, api=None):
    api = api or FakeCalendarAPI()
    return CalendarSync(app_session_factory, clinic_id, api=api), api


def book(app_session_factory, clinic_id, doctor_id, service_id, day, hhmm,
         chat_id=100, patient_id=None):
    sched = SchedulingEngine(app_session_factory, clinic_id)
    appointment_id = sched.hold(doctor_id, service_id, at_tashkent(day, hhmm),
                                patient_id=patient_id, tg_chat_id=chat_id)
    sched.confirm(appointment_id)
    return appointment_id, sched


def event_row(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT gcal_event_id, gcal_synced_range, time_range FROM appointment"
        )).one()


def test_booked_appointment_exported_once(app_session_factory, admin_engine,
                                          clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00")
    sync, api = make_sync(app_session_factory, clinic_a)

    sync.sync_doctor(doctor_a)
    events = list(api.events().values())
    assert len(events) == 1
    event = events[0]
    assert "cleaning" in event["summary"].lower() or "Чистка" in event["summary"]
    appointment = event_row(admin_engine)
    assert event["extendedProperties"]["private"]["navbat_id"]
    assert appointment.gcal_event_id == event["id"]

    sync.sync_doctor(doctor_a)  # повторный прогон — без дублей и patch'ей
    assert api.insert_calls == 1
    assert api.patch_calls == 0


def test_cancelled_appointment_removes_event(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, next_monday(), "09:00")
    sync, api = make_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)

    sched.cancel(appointment_id)
    sync.sync_doctor(doctor_a)

    assert all(e["status"] == "cancelled" for e in api.events().values())
    assert event_row(admin_engine).gcal_event_id is None


def test_bot_reschedule_patches_event(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, next_monday(), "09:00")
    sync, api = make_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)

    new_start = at_tashkent(next_monday(), "11:00")
    sched.reschedule(appointment_id, new_start)
    sync.sync_doctor(doctor_a)

    assert api.insert_calls == 1, "перенос — это patch, не новое событие"
    assert api.patch_calls == 1
    event = next(iter(api.events().values()))
    assert event["start"]["dateTime"] == new_start.isoformat()


# ── Тело события: услуга по-русски, имя, телефон (полировка-3 Г) ──────────

def test_export_event_has_label_name_and_phone(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    """Владелец живёт в календаре: событие отвечает «кто и зачем»."""
    bind_calendar(admin_engine, doctor_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        patient_id = create_patient(session, tg_chat_id=100, name="Алишер",
                                    phone="+998 90 123-45-67")
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00", patient_id=patient_id)
    sync, api = make_sync(app_session_factory, clinic_a)

    sync.sync_doctor(doctor_a)

    event = next(iter(api.events().values()))
    assert event["summary"] == "Чистка — Алишер (Navbat)"
    assert "Телефон: +998901234567" in event["description"]


def test_export_event_without_patient_degrades(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    """Запись без пациента (брошенный путь) — без имени и телефона."""
    bind_calendar(admin_engine, doctor_a)
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00")
    sync, api = make_sync(app_session_factory, clinic_a)

    sync.sync_doctor(doctor_a)

    event = next(iter(api.events().values()))
    assert event["summary"] == "Чистка (Navbat)"
    assert "description" not in event


def test_export_event_patient_with_null_pii_degrades(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """NULL-колонки (старые пациенты до 0018, /forget) — строки пропущены."""
    bind_calendar(admin_engine, doctor_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        patient_id = create_patient(session, tg_chat_id=100, name="Алишер",
                                    phone="901234567")
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE patient SET name_encrypted = NULL, "
                          "phone_encrypted = NULL WHERE id = :id"),
                     {"id": patient_id})
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00", patient_id=patient_id)
    sync, api = make_sync(app_session_factory, clinic_a)

    sync.sync_doctor(doctor_a)

    event = next(iter(api.events().values()))
    assert event["summary"] == "Чистка (Navbat)"
    assert "description" not in event


def test_doctor_without_calendar_is_skipped(app_session_factory, admin_engine,
                                            clinic_a, doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00")
    sync, api = make_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)  # календарь не привязан
    assert api.insert_calls == 0
