"""Импорт ручных событий GCal: они — истина и закрывают слоты бота.

Свои события (navbat_id) — наоборот: истина в БД, ручная правка
откатывается с алертом.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.scheduling.engine import SchedulingEngine
from test_dialog_booking import RecordingNotifier
from test_gcal_export import (
    CAL,
    FakeCalendarAPI,
    bind_calendar,
    book,
    make_sync,
)
from navbat.calendar.sync import CalendarSync


def free_starts(app_session_factory, clinic_id, doctor_id, service_id, day):
    sched = SchedulingEngine(app_session_factory, clinic_id)
    return {s.start for s in sched.find_free_slots(doctor_id, service_id, day)}


def import_rows(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT status, gcal_event_id, lower(time_range) AS start "
            "FROM appointment WHERE source = 'gcal_import' ORDER BY created_at"
        )).all()


def test_manual_event_blocks_slot(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sync, api = make_sync(app_session_factory, clinic_a)
    api.seed_manual_event(start=at_tashkent(day, "09:00"), end=at_tashkent(day, "09:30"))

    sync.sync_doctor(doctor_a)

    rows = import_rows(admin_engine)
    assert len(rows) == 1 and rows[0].status == "booked"
    starts = free_starts(app_session_factory, clinic_a, doctor_a, service_cleaning, day)
    assert at_tashkent(day, "09:00") not in starts

    sync.sync_doctor(doctor_a)  # идемпотентность
    assert len(import_rows(admin_engine)) == 1


def test_all_day_event_blocks_whole_day(app_session_factory, admin_engine, clinic_a,
                                        doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sync, api = make_sync(app_session_factory, clinic_a)
    api.seed_manual_event(day=day, summary="Отпуск")

    sync.sync_doctor(doctor_a)
    assert free_starts(app_session_factory, clinic_a, doctor_a,
                       service_cleaning, day) == set()


def test_deleted_manual_event_frees_slot(app_session_factory, admin_engine, clinic_a,
                                         doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sync, api = make_sync(app_session_factory, clinic_a)
    event_id = api.seed_manual_event(start=at_tashkent(day, "09:00"),
                                     end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    api.delete_event(CAL, event_id)
    sync.sync_doctor(doctor_a)

    assert import_rows(admin_engine)[0].status == "cancelled"
    starts = free_starts(app_session_factory, clinic_a, doctor_a, service_cleaning, day)
    assert at_tashkent(day, "09:00") in starts


def test_moved_manual_event_moves_block(app_session_factory, admin_engine, clinic_a,
                                        doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sync, api = make_sync(app_session_factory, clinic_a)
    event_id = api.seed_manual_event(start=at_tashkent(day, "09:00"),
                                     end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    api.events(CAL)[event_id]["start"] = {"dateTime": at_tashkent(day, "15:00").isoformat()}
    api.events(CAL)[event_id]["end"] = {"dateTime": at_tashkent(day, "15:30").isoformat()}
    sync.sync_doctor(doctor_a)

    starts = free_starts(app_session_factory, clinic_a, doctor_a, service_cleaning, day)
    assert at_tashkent(day, "09:00") in starts, "старый блок снят"
    assert at_tashkent(day, "15:00") not in starts, "новый блок встал"


# ── Свои события: истина в БД ────────────────────────────────────────────────

def test_manually_deleted_bot_event_is_recreated(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    notifier = RecordingNotifier()
    api = FakeCalendarAPI()
    sync = CalendarSync(app_session_factory, clinic_a, api=api, notifier=notifier)
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00")
    sync.sync_doctor(doctor_a)
    bot_event_id = next(iter(api.events(CAL)))

    api.delete_event(CAL, bot_event_id)  # админ руками снёс событие бота
    sync.sync_doctor(doctor_a)

    alive = [e for e in api.events(CAL).values() if e["status"] == "confirmed"]
    assert len(alive) == 1, "событие пересоздано — запись в БД жива"
    assert notifier.calls, "админ предупреждён: правка записей — через бота"


def test_manually_moved_bot_event_is_restored(app_session_factory, admin_engine,
                                              clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    notifier = RecordingNotifier()
    api = FakeCalendarAPI()
    sync = CalendarSync(app_session_factory, clinic_a, api=api, notifier=notifier)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00")
    sync.sync_doctor(doctor_a)
    bot_event_id = next(iter(api.events(CAL)))

    api.events(CAL)[bot_event_id]["start"] = {"dateTime": at_tashkent(day, "16:00").isoformat()}
    api.events(CAL)[bot_event_id]["end"] = {"dateTime": at_tashkent(day, "16:30").isoformat()}
    sync.sync_doctor(doctor_a)

    assert api.events(CAL)[bot_event_id]["start"]["dateTime"] == \
        at_tashkent(day, "09:00").isoformat()
    assert notifier.calls


# ── syncToken ────────────────────────────────────────────────────────────────

def test_sync_token_is_persisted_and_reused(app_session_factory, admin_engine,
                                            clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    sync, api = make_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)
    sync.sync_doctor(doctor_a)

    assert api.list_tokens[0] is None, "первый запуск — full sync"
    assert api.list_tokens[1] == "SYNC1", "дальше — инкрементально"
    with admin_engine.begin() as conn:
        token = conn.execute(text("SELECT gcal_sync_token FROM doctor")).scalar_one()
    assert token == "SYNC2"
