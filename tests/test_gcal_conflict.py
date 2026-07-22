"""Приёмочный тест BRIEF: ручное событие поверх записи бота.

Приоритет у ручного (человек физически в кресле): бот переносит свою
запись на ближайший слот, уведомляет пациента (с кнопками альтернатив)
и админа. Не молчаливое отклонение.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import at_tashkent, make_doctor, next_monday
from navbat.calendar.sync import CalendarSync
from navbat.scheduling.engine import SchedulingEngine
from test_dialog_booking import CHAT, RecordingNotifier
from test_gcal_export import CAL, FakeCalendarAPI, bind_calendar, book
from test_tg_worker import FakeTelegramAPI


def make_conflict_sync(app_session_factory, clinic_id):
    api = FakeCalendarAPI()
    notifier = RecordingNotifier()
    tg_api = FakeTelegramAPI()
    sync = CalendarSync(app_session_factory, clinic_id, api=api,
                        notifier=notifier, tg_api=tg_api)
    return sync, api, notifier, tg_api


def bot_rows(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT status, lower(time_range) AS start FROM appointment "
            "WHERE source = 'bot' ORDER BY created_at"
        )).all()


def import_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM appointment "
            "WHERE source = 'gcal_import' AND status = 'booked'"
        )).scalar_one()


def test_manual_event_over_booking_moves_it_and_notifies(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)  # событие бота уехало в календарь

    # админ руками вписал пациента с улицы прямо поверх
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    # ручной блок встал
    assert import_count(admin_engine) == 1
    # ботовская переехала: старая отменена, новая — на 10:00
    # (09:30 не подходит: буфер 10 мин после ручного приёма)
    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["cancelled", "booked"]
    assert rows[1].start == at_tashkent(day, "10:00")

    # пациент уведомлён, с кнопками альтернатив
    assert tg_api.sent, "пациент должен получить уведомление о переносе"
    chat_id, message_text, buttons = tg_api.sent[-1]
    assert chat_id == CHAT
    assert "10:00" in message_text
    assert buttons, "кнопки альтернативных слотов"
    with admin_engine.begin() as conn:
        conv = conn.execute(text(
            "SELECT fsm_state, context FROM conversation WHERE tg_chat_id = :c"),
            {"c": CHAT}).one()
    assert conv.fsm_state == "resched_offer_slots"
    assert conv.context["tg_actions"]["1"].startswith("reslot:")

    # админ уведомлён
    assert notifier.calls

    # календарь согласован: старое событие бота удалено, новое — на 10:00
    confirmed = [e for e in api.events(CAL).values()
                 if e["status"] == "confirmed" and "extendedProperties" in e]
    assert len(confirmed) == 1
    assert confirmed[0]["start"]["dateTime"] == at_tashkent(day, "10:00").isoformat()


def test_no_alternative_slot_cancels_and_notifies(app_session_factory, admin_engine,
                                                  clinic_a, service_cleaning):
    # врач работает один слот в неделю: пн 09:00–09:30
    doctor = make_doctor(admin_engine, clinic_a,
                         intervals={"mon": [["09:00", "09:30"]]})
    bind_calendar(admin_engine, doctor)
    day = next_monday()
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)

    # будущие понедельники заняты заранее (скан переноса — 14 дней)
    from datetime import timedelta
    api.seed_manual_event(day=day + timedelta(days=7))
    api.seed_manual_event(day=day + timedelta(days=14))
    sync.sync_doctor(doctor)

    book(app_session_factory, clinic_a, doctor, service_cleaning, day, "09:00",
         chat_id=CHAT)
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor)

    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["cancelled"], "переносить некуда — отмена"
    assert import_count(admin_engine) == 3
    assert tg_api.sent and not tg_api.sent[-1][2], "уведомление без кнопок"
    assert notifier.calls


def test_conflict_with_live_hold_waits_for_expiry(app_session_factory, admin_engine,
                                                  clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sched = SchedulingEngine(app_session_factory, clinic_a)
    sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"), tg_chat_id=CHAT)

    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    # живой hold не трогаем — пациент прямо сейчас выбирает; импорт отложен
    assert import_count(admin_engine) == 0
    with admin_engine.begin() as conn:
        status = conn.execute(text(
            "SELECT status FROM appointment WHERE source = 'bot'")).scalar_one()
    assert status == "hold"

    # hold протух — следующий цикл дотащил импорт
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE appointment SET hold_expires_at = "
                          "now() - interval '1 minute' WHERE status = 'hold'"))
    sync.sync_doctor(doctor_a)
    assert import_count(admin_engine) == 1
