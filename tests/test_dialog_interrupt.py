"""Прерывание вопросом вбок посреди записи: ответ + возврат к шагу, не сброс."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from conftest import make_service, next_monday
from navbat.dialog.dialog_common import NEAREST_DAY_SCAN
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from test_dialog_booking import (
    CHAT,
    explicit,
    extr,
    fsm_state,
    make_engine,
    slot_buttons,
)


def test_question_during_slot_offer_keeps_state(app_session_factory, admin_engine,
                                                clinic_a, doctor_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(service="cleaning", date_ref=explicit(next_monday())),
        extr(intent="question", service="cleaning"),
    ]))
    engine.handle_text(CHAT, "чистку в понедельник")
    reply = engine.handle_text(CHAT, "а сколько это стоит?")

    assert "350 000" in reply.text, "ответ на вопрос"
    assert slot_buttons(reply), "и тут же — возврат к выбору слота"
    assert fsm_state(admin_engine) == "booking_offer_slots"


def test_question_during_name_collection_reasks_name(app_session_factory, admin_engine,
                                                     clinic_a, doctor_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(service="cleaning", date_ref=explicit(next_monday())),
        extr(intent="question", service="cleaning"),
    ]))
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    reply = engine.handle_text(CHAT, "сколько стоит чистка?")
    assert "350 000" in reply.text
    assert fsm_state(admin_engine) == "awaiting_name", "шаг не сброшен"

    # следующий ответ снова трактуется как имя
    after = engine.handle_text(CHAT, "Алишер")
    assert fsm_state(admin_engine) == "awaiting_phone"
    assert not after.buttons


def test_question_during_no_slots_step_keeps_calendar_grid(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    """Шаг «нет слотов» держит клавиатуру в button_rows (сетка месяца), плоских
    buttons у него нет. Прерывание вбок обязано донести шаг ЦЕЛИКОМ: ручная
    сборка Reply переносила только text и buttons — пациент получал текст без
    единой кнопки, хотя состояние ждёт выбора дня (тупик записи)."""
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    monday = next_monday()
    # весь горизонт поиска слотов (NEAREST_DAY_SCAN дней от спрошенного дня) —
    # выходные клиники: шаг записи уходит в месячную сетку. Способ тот же, что
    # в test_empty_month_grid_shows_note
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO holiday (clinic_id, date) "
            "SELECT :cid, d::date FROM generate_series("
            "CAST(:a AS timestamp), CAST(:b AS timestamp), '1 day') d"),
            {"cid": clinic_a, "a": monday,
             "b": monday + timedelta(days=NEAREST_DAY_SCAN)})
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(monday)),
        extr(intent="question", service="cleaning"),
    ])
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    assert offer.button_rows and not offer.buttons, \
        "шаг «нет слотов» отдаёт клавиатуру рядами, не плоским списком"

    reply = engine.handle_text(CHAT, "а сколько это стоит?")

    assert "350 000" in reply.text, "ответ на вопрос"
    assert TEMPLATES["no_slots_calendar"]["ru"] in reply.text, \
        "и тут же — текст текущего шага"
    assert reply.button_rows, "сетка календаря доехала до пациента"
    assert any(b.action == "wl:join:cleaning"
               for row in reply.button_rows for b in row), \
        "и кнопка очереди из того же шага"
    assert fsm_state(admin_engine) == "booking_collect", "шаг не сброшен"
