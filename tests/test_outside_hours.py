"""П-3: запрошенное время вне рабочего окна → честная строка
«Клиника работает с X до Y» ПЕРЕД слотами (находка живого теста 10.06:
«а в 21 могу?» получал слоты на утро без объяснения).
"""
from __future__ import annotations

from datetime import timedelta

from conftest import at_tashkent, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, slot_buttons)
from test_dialog_reschedule_cancel import book_directly

HOURS_LINE = "Клиника работает с 09:00 до 18:00."


def make(app_session_factory, clinic_id, script, clock):
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=script),
                        notifier=RecordingNotifier(), clock=clock)


def booking_engine(app_session_factory, clinic_a, time_ref, asked_day, clock):
    return make(app_session_factory, clinic_a,
                [extr(service="cleaning", date_ref=explicit(asked_day),
                      time_ref=time_ref)], clock)


def test_late_evening_request_explains_hours(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine = booking_engine(app_session_factory, clinic_a, "21:00", monday,
                            clock=lambda: at_tashkent(monday - timedelta(days=3),
                                                      "10:00"))
    reply = engine.handle_text(CHAT, "а в 21:00 в понедельник можно?")

    assert HOURS_LINE in reply.text
    assert slot_buttons(reply), "слоты всё равно предложены"


def test_early_morning_request_explains_hours(app_session_factory, admin_engine,
                                              clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine = booking_engine(app_session_factory, clinic_a, "06:00", monday,
                            clock=lambda: at_tashkent(monday - timedelta(days=3),
                                                      "10:00"))
    reply = engine.handle_text(CHAT, "в понедельник в 6 утра")
    assert HOURS_LINE in reply.text


def test_time_inside_window_has_no_line(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine = booking_engine(app_session_factory, clinic_a, "14:00", monday,
                            clock=lambda: at_tashkent(monday - timedelta(days=3),
                                                      "10:00"))
    reply = engine.handle_text(CHAT, "в понедельник в 14:00")
    assert "Клиника работает" not in reply.text


def test_time_window_word_has_no_line(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    # «вечером» — окно, не точное время: строка не нужна
    monday = next_monday()
    engine = booking_engine(app_session_factory, clinic_a, "evening", monday,
                            clock=lambda: at_tashkent(monday - timedelta(days=3),
                                                      "10:00"))
    reply = engine.handle_text(CHAT, "в понедельник вечером")
    assert "Клиника работает" not in reply.text


def test_closed_now_not_duplicated(app_session_factory, admin_engine,
                                   clinic_a, doctor_a, service_cleaning):
    # «сегодня в 21» поздно вечером: «Сейчас клиника закрыта» уже объясняет —
    # вторая строка о часах была бы шумом
    monday = next_monday()
    engine = booking_engine(app_session_factory, clinic_a, "21:00", monday,
                            clock=lambda: at_tashkent(monday, "20:00"))
    reply = engine.handle_text(CHAT, "сегодня в 21 можно?")

    assert "Сейчас клиника закрыта" in reply.text
    assert "Клиника работает" not in reply.text


def test_reschedule_off_hours_also_explains(app_session_factory, admin_engine,
                                            clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    tuesday = monday + timedelta(days=1)
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  monday, "09:00")
    engine = make(app_session_factory, clinic_a,
                  [extr(intent="reschedule", date_ref=explicit(tuesday),
                        time_ref="21:30")],
                  clock=lambda: at_tashkent(monday - timedelta(days=3), "10:00"))
    reply = engine.handle_text(CHAT, "перенесите на вторник на 21:30")
    assert HOURS_LINE in reply.text
