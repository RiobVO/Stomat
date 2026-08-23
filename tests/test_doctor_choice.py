"""Время — один раз, врач — отдельным шагом и только когда есть из чего выбрать.

Врач попадал в ключ отображения слота, и временна́я ось множилась на число
врачей: сетка дня печатала каждое время дважды (32 кнопки, по две в ряд —
простыня на экране телефона), а короткое предложение из четырёх кнопок
показывало всего ДВА реальных времени — половина уходила на дубли.
Пациент в этот момент выбирает КОГДА; кто именно — видно в подтверждении.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from conftest import at_tashkent, make_doctor, make_service, next_monday
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


def test_old_time_button_does_not_become_a_doctor_button(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Вопрос «к кому» приходит СРАЗУ после сетки времени и перезаписывает
    карту кнопок чата — а сетка остаётся на экране.

    Пронумерованная кнопка «09:30» из неё превращалась в кнопку врача из
    следующего сообщения: пациент жал 09:30, а бронировалось 09:00.
    """
    from navbat.telegram.worker import UpdateWorker, send_reply
    from test_dialog_booking import RecordingNotifier
    from test_tg_worker import FakeTelegramAPI, put_callback

    two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    api = FakeTelegramAPI()
    worker = UpdateWorker(app_session_factory, clinic_a, dialog=engine,
                          api=api, notifier=RecordingNotifier())

    grid = open_day(engine, monday)
    send_reply(api, app_session_factory, clinic_a, CHAT, grid)
    sent_grid = api.row_keyboards[-1]
    second = [b for row in sent_grid for b in row][1]  # кнопка «09:30»
    assert second.label == "09:30"

    # пациент тапнул 09:00 → пришёл вопрос о враче и перезаписал карту
    ask = engine.handle_action(CHAT, time_buttons(grid)[0].action)
    send_reply(api, app_session_factory, clinic_a, CHAT, ask)

    # ...а он вернулся к старому сообщению и нажал 09:30
    put_callback(app_session_factory, clinic_a, second.action)
    worker.process_one()

    with admin_engine.begin() as conn:
        starts = conn.execute(text(
            "SELECT lower(time_range) FROM appointment "
            "WHERE status IN ('hold', 'booked')")).scalars().all()
    assert at_tashkent(monday, "09:00") not in starts, \
        "тап по 09:30 забронировал 09:00 — карта кнопок перезаписана"


def test_taken_doctor_is_not_silently_swapped(app_session_factory, admin_engine,
                                              clinic_a, doctor_a,
                                              service_cleaning):
    """Выбранного врача заняли, пока пациент отвечал.

    Молча посадить его к другому нельзя — он выбирал ЭТОГО. Бот обязан
    сказать и предложить свободных."""
    from navbat.scheduling.engine import SchedulingEngine

    first, second = two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    reply = open_day(engine, monday)
    ask = engine.handle_action(CHAT, time_buttons(reply)[0].action)
    mine = next(b for b in flat(ask.button_rows)
                if b.action.startswith("d:") and "any" not in b.action)
    chosen = mine.action.split(":")[1]

    # чужая бронь заняла именно выбранного врача
    other = SchedulingEngine(app_session_factory, clinic_a)
    other.hold(uuid.UUID(chosen), service_cleaning,
               at_tashkent(monday, "09:00"), tg_chat_id=CHAT + 500)

    answer = engine.handle_action(CHAT, mine.action)

    with admin_engine.begin() as conn:
        mine_booked = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE tg_chat_id = :c "
            "AND status IN ('hold', 'booked')"), {"c": CHAT}).scalar_one()
    assert mine_booked == 0, "пациента молча посадили к другому врачу"
    assert answer.button_rows or answer.buttons, "бот обязан дать выбор"


def test_huge_minutes_do_not_crash_the_handler(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    """Минуты приходят из callback_data, то есть от клиента: переполнение
    роняло обработку, и апдейт уходил в dead letter."""
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday)
    open_day(engine, monday)

    assert engine.handle_action(CHAT, "d:any:" + "9" * 40).text


def test_time_button_from_a_past_grid_does_not_book_backwards(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Кнопка времени стала сырой — значит, живёт в чате сколько угодно.

    Движок сверяет старт только с рабочей сеткой врача, а не с текущим
    моментом: валидное «09:00 прошлого понедельника» на сетке лежит, и тап
    по вчерашнему сообщению записывал бы пациента в прошлое. Та же дыра,
    что уже находилась у reslot:."""
    monday = next_monday()
    past = monday - timedelta(days=14)
    engine = engine_on(app_session_factory, clinic_a, monday)
    open_day(engine, monday)

    engine.handle_action(
        CHAT, f"time:{at_tashkent(past, '09:00').isoformat()}")

    with admin_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT count(*) FROM appointment "
            "WHERE lower(time_range) < now()")).scalar_one()
    assert rows == 0, "запись оформлена в прошлое"


def test_doctor_button_from_a_past_grid_does_not_book_backwards(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """То же для кнопки врача: она несёт минуты эпохи от клиента."""
    monday = next_monday()
    past = monday - timedelta(days=14)
    engine = engine_on(app_session_factory, clinic_a, monday)
    open_day(engine, monday)
    minutes = int(at_tashkent(past, "09:00").timestamp()) // 60

    engine.handle_action(CHAT, f"d:any:{minutes}")

    with admin_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT count(*) FROM appointment "
            "WHERE lower(time_range) < now()")).scalar_one()
    assert rows == 0, "запись оформлена в прошлое"


def _service_of_booking(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT s.name FROM appointment a JOIN service s ON s.id = a.service_id "
            "WHERE a.status IN ('hold', 'booked')")).scalar_one()


def test_old_time_button_keeps_its_own_service(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    """Кнопка несла время, но услугу брала из ТЕКУЩЕГО контекста.

    Пациент выбрал время на чистку, потом начал новый сценарий (пломба), а
    затем вернулся к старому сообщению и тапнул время оттуда — записывался
    на пломбу. Сырой callback обязан нести свой субъект целиком."""
    make_service(admin_engine, clinic_a, "filling", 60)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday, script=[
        extr(service="cleaning", date_ref=explicit(monday)),
        extr(service="filling", date_ref=explicit(monday)),
    ])
    grid = open_day(engine, monday)
    old_button = time_buttons(grid)[0].action

    engine.handle_text(CHAT, "а лучше пломбу")  # новый сценарий
    engine.handle_action(CHAT, old_button)      # тап по старому сообщению

    assert _service_of_booking(admin_engine) == "cleaning", \
        "тап по кнопке из сообщения про чистку записал на другую услугу"


def test_old_doctor_button_keeps_its_own_service(app_session_factory,
                                                 admin_engine, clinic_a,
                                                 doctor_a, service_cleaning):
    """То же для кнопки врача: она живёт в чате и переживает смену сценария."""
    make_service(admin_engine, clinic_a, "filling", 60)
    two_doctors(admin_engine, clinic_a, doctor_a)
    monday = next_monday()
    engine = engine_on(app_session_factory, clinic_a, monday, script=[
        extr(service="cleaning", date_ref=explicit(monday)),
        extr(service="filling", date_ref=explicit(monday)),
    ])
    grid = open_day(engine, monday)
    ask = engine.handle_action(CHAT, time_buttons(grid)[0].action)
    old_doctor_button = next(b.action for b in flat(ask.button_rows)
                             if b.action.startswith("d:any:"))

    engine.handle_text(CHAT, "а лучше пломбу")
    engine.handle_action(CHAT, old_doctor_button)

    assert _service_of_booking(admin_engine) == "cleaning", \
        "кнопка врача из сообщения про чистку записала на другую услугу"
