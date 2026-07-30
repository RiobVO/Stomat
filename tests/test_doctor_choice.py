"""Время — один раз, врач — отдельным шагом и только когда есть из чего выбрать.

Врач попадал в ключ отображения слота, и временна́я ось множилась на число
врачей: сетка дня печатала каждое время дважды (32 кнопки, по две в ряд —
простыня на экране телефона), а короткое предложение из четырёх кнопок
показывало всего ДВА реальных времени — половина уходила на дубли.
Пациент в этот момент выбирает КОГДА; кто именно — видно в подтверждении.
"""
from __future__ import annotations

from datetime import datetime

from conftest import at_tashkent, make_doctor, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from sqlalchemy import text
from test_dialog_booking import CHAT, explicit, extr, fsm_state


def flat(rows):
    return [b for row in rows for b in row]


def two_doctors(admin_engine, clinic_a, doctor_a):
    """Второй врач с тем же графиком — оба свободны в одни и те же часы."""
    return doctor_a, make_doctor(admin_engine, clinic_a, name="Akmal aka")


def engine_on(app_session_factory, clinic_a, monday, script=None):
    return DialogEngine(
        app_session_factory, clinic_a,
        extractor=FakeExtractor(script=script or [
            extr(service="cleaning", date_ref=explicit(monday))]),
        clock=lambda: at_tashkent(monday, "08:00"))


def open_day(engine, monday):
    engine.handle_text(CHAT, "чистка в понедельник")
    return engine.handle_action(CHAT, f"cal:day:{monday.isoformat()}")


def time_buttons(reply):
    return [b for b in flat(reply.button_rows)
            if b.action.startswith("time:")]


def test_day_grid_prints_each_time_once(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    """Два врача с одинаковым графиком — сетка обязана остаться одной длины."""
    two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    reply = open_day(engine_on(app_session_factory, clinic_a, monday), monday)

    buttons = time_buttons(reply)
    labels = [b.label for b in buttons]
    assert len(labels) == 16, "график 09–13/14–18 по 30 мин — ровно 16 времён"
    assert len(set(labels)) == len(labels), "время напечатано дважды"
    assert all("·" not in label for label in labels), \
        "имя врача в кнопке времени — оно удваивает ширину и ряды"
    assert all(len(row) <= 4 for row in reply.button_rows)


def test_short_offer_gives_four_distinct_times(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    """Лимит короткого предложения — четыре кнопки; все четыре обязаны быть
    РАЗНЫМ временем, иначе выбор вдвое беднее заявленного."""
    two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    reply = engine.handle_text(CHAT, "чистка в понедельник")

    labels = [b.label for b in reply.buttons if b.action.startswith("time:")]
    assert len(set(labels)) == 4, f"разных времён в предложении: {labels}"


def test_time_with_two_free_doctors_asks_which_one(app_session_factory,
                                                   admin_engine, clinic_a,
                                                   doctor_a, service_cleaning):
    first, second = two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    reply = open_day(engine, monday)

    ask = engine.handle_action(CHAT, time_buttons(reply)[0].action)
    actions = [b.action for b in flat(ask.button_rows) or ask.buttons]
    assert sum(a.startswith("d:") for a in actions) == 3, \
        "оба врача плюс «Любой»"
    assert any(a.startswith("d:any:") for a in actions), "кнопка «Любой»"
    assert str(first) in " ".join(actions) and str(second) in " ".join(actions)
    assert all(len(a.encode()) <= 64 for a in actions), "лимит callback_data"


def test_time_with_single_free_doctor_does_not_ask(app_session_factory,
                                                   admin_engine, clinic_a,
                                                   doctor_a, service_cleaning):
    """Врач один — лишнего шага быть не должно: сразу дальше по сценарию."""
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    reply = open_day(engine, monday)

    engine.handle_action(CHAT, time_buttons(reply)[0].action)
    assert fsm_state(admin_engine) == "awaiting_name", \
        "у одного врача выбор врача не нужен"


def test_any_doctor_books_one_of_the_free(app_session_factory, admin_engine,
                                          clinic_a, doctor_a, service_cleaning):
    two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    reply = open_day(engine, monday)
    ask = engine.handle_action(CHAT, time_buttons(reply)[0].action)
    any_btn = next(b for b in flat(ask.button_rows) or ask.buttons
                   if b.action.startswith("d:any:"))

    engine.handle_action(CHAT, any_btn.action)

    with admin_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT doctor_id, lower(time_range) AS start FROM appointment "
            "WHERE status IN ('hold', 'booked')")).one()
    assert row.doctor_id is not None
    assert row.start == at_tashkent(monday, "09:00")
    assert fsm_state(admin_engine) == "awaiting_name"


def test_doctor_pick_survives_a_garbage_callback(app_session_factory,
                                                 admin_engine, clinic_a,
                                                 doctor_a, service_cleaning):
    """callback_data приходит от клиента: мусор гасится, а не роняет диалог."""
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    open_day(engine, monday)

    assert engine.handle_action(CHAT, "d:не-uuid:12345").text
    assert engine.handle_action(CHAT, "time:не-время").text
