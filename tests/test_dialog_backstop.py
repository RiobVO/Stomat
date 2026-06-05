"""book↔question бэкстоп и ответы на вопросы.

Известная дыра NLU: косвенный вопрос о наличии («есть время сегодня?»)
уходит в question — FSM обязан ответить слотами, не текстом.
"""
from __future__ import annotations

from conftest import make_service, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT,
    RecordingNotifier,
    explicit,
    extr,
    fsm_state,
    slot_buttons,
)


def test_availability_question_answered_with_slots(app_session_factory, admin_engine,
                                                   clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(intent="question", service=None, date_ref=explicit(next_monday())),
    ]))
    reply = engine.handle_text(CHAT, "есть время в понедельник?")
    assert slot_buttons(reply), "вопрос о наличии — всегда ответ слотами"


def test_price_question_answered_from_db(app_session_factory, admin_engine, clinic_a,
                                         doctor_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(intent="question", service="cleaning"),
    ]))
    reply = engine.handle_text(CHAT, "сколько стоит чистка?")
    assert "350 000" in reply.text
    assert fsm_state(admin_engine) == "idle"


def test_price_unknown_defers_to_admin(app_session_factory, admin_engine, clinic_a,
                                       doctor_a, service_cleaning):
    # цена в каталоге не заполнена
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(intent="question", service="cleaning"),
    ]))
    reply = engine.handle_text(CHAT, "сколько стоит чистка?")
    assert "администратор" in reply.text.lower()


def test_general_question_falls_back_and_notifies(app_session_factory, admin_engine,
                                                  clinic_a, doctor_a, service_cleaning):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="question")]),
                          notifier=notifier)
    reply = engine.handle_text(CHAT, "а где вы находитесь?")
    assert not reply.buttons
    assert len(notifier.calls) == 1
    # вопрос без записи — не эскалация диалога, бот продолжает работать
    assert fsm_state(admin_engine) == "idle"
