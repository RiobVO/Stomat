"""Прерывание вопросом вбок посреди записи: ответ + возврат к шагу, не сброс."""
from __future__ import annotations

from conftest import make_service, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from test_dialog_booking import (
    CHAT,
    explicit,
    extr,
    fsm_state,
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
