"""«Сейчас закрыто»: запрос «на сегодня» вне рабочих часов → явный ответ (P0).

Семантика: closed_now_slots появляется только когда пациент метит в сегодня
(start_day == today), а «сейчас» — вне рабочего окна дня клиники
[min(начал); max(концов)] по всем врачам. Обед — НЕ «закрыто», запрос
на будущую дату ночью — обычные слоты. Время инжектируется через clock —
тесты не зависят от момента запуска.
График фикстуры: пн–сб 09:00–13:00 и 14:00–18:00 (conftest.WORKING_INTERVALS).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from conftest import at_tashkent, next_monday, next_sunday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT,
    RecordingNotifier,
    explicit,
    extr,
    slot_buttons,
    slot_start,
)
from test_dialog_reschedule_cancel import book_directly


def engine_at(app_session_factory, clinic_id, script, when) -> DialogEngine:
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=script),
                        notifier=RecordingNotifier(),
                        clock=lambda: when)


def closed_text(date) -> str:
    return TEMPLATES["closed_now_slots"]["ru"].format(date=f"{date:%d.%m}")


def offer_text(date) -> str:
    return TEMPLATES["offer_slots"]["ru"].format(date=f"{date:%d.%m}")


def add_holiday(admin_engine, clinic_id, day) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO holiday (clinic_id, date, reason) "
                 "VALUES (:c, :d, 'тестовый праздник')"),
            {"c": clinic_id, "d": day},
        )


# ── «Сегодня» вне рабочего окна → явное «закрыто» ────────────────────────────

def test_evening_today_request_says_closed_offers_next_day(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref="today")],
                       at_tashkent(day, "22:00"))
    reply = engine.handle_text(CHAT, "болит зуб, можно сегодня?")

    assert closed_text(day + timedelta(days=1)) in reply.text
    slots = slot_buttons(reply)
    assert slots, "запись остаётся возможной — слоты следующего дня"
    assert slot_start(slots[0]) == at_tashkent(day + timedelta(days=1), "09:00")


def test_early_morning_says_closed_offers_today(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref="today")],
                       at_tashkent(day, "07:00"))
    reply = engine.handle_text(CHAT, "можно сегодня с утра?")

    assert closed_text(day) in reply.text, "клиника ещё не открылась, но слоты сегодня"
    assert slot_start(slot_buttons(reply)[0]) == at_tashkent(day, "09:00")


def test_sunday_today_request_says_closed(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_sunday()  # воскресенья нет в working_intervals
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref="today")],
                       at_tashkent(day, "12:00"))
    reply = engine.handle_text(CHAT, "запишите на сегодня")

    assert closed_text(day + timedelta(days=1)) in reply.text
    assert slot_start(slot_buttons(reply)[0]) == at_tashkent(
        day + timedelta(days=1), "09:00")


def test_holiday_today_request_says_closed(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    add_holiday(admin_engine, clinic_a, day)
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref="today")],
                       at_tashkent(day, "12:00"))
    reply = engine.handle_text(CHAT, "можно сегодня?")

    assert closed_text(day + timedelta(days=1)) in reply.text


def test_reschedule_today_at_night_says_closed(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  day + timedelta(days=1), "14:00")
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(intent="reschedule", date_ref="today")],
                       at_tashkent(day, "22:00"))
    reply = engine.handle_text(CHAT, "перенесите на сегодня")

    assert closed_text(day + timedelta(days=1)) in reply.text


# ── Рабочее время и будущие даты → без «закрыто» ─────────────────────────────

def test_lunch_break_is_not_closed(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref="today")],
                       at_tashkent(day, "13:30"))
    reply = engine.handle_text(CHAT, "можно сегодня?")

    assert offer_text(day) in reply.text, "обед — клиника открыта, обычные слоты"
    assert TEMPLATES["closed_now_slots"]["ru"].splitlines()[0] not in reply.text
    assert slot_start(slot_buttons(reply)[0]) == at_tashkent(day, "14:00")


def test_working_hours_no_closed_prefix(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref="today")],
                       at_tashkent(day, "10:00"))
    reply = engine.handle_text(CHAT, "можно сегодня?")

    assert offer_text(day) in reply.text
    assert TEMPLATES["closed_now_slots"]["ru"].splitlines()[0] not in reply.text


def test_future_date_at_night_no_closed_prefix(
        app_session_factory, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    wednesday = day + timedelta(days=2)
    engine = engine_at(app_session_factory, clinic_a,
                       [extr(service="cleaning", date_ref=explicit(wednesday))],
                       at_tashkent(day, "22:00"))
    reply = engine.handle_text(CHAT, "запишите на среду")

    assert offer_text(wednesday) in reply.text
    assert TEMPLATES["closed_now_slots"]["ru"].splitlines()[0] not in reply.text
