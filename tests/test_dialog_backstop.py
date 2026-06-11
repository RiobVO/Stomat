"""book↔{question,other} бэкстоп и ответы на вопросы.

Известная дыра NLU: косвенный вопрос о наличии («есть время сегодня?»)
уходит в question, голое «на завтра» — в other; реплика с привязкой
ко времени — это про запись, FSM обязан вести сценарий, не текстом.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday
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


def ctx_date(admin_engine, chat_id=CHAT) -> str | None:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT context ->> 'date' FROM conversation WHERE tg_chat_id = :c"),
            {"c": chat_id},
        ).scalar_one()


def test_availability_question_answered_with_slots(app_session_factory, admin_engine,
                                                   clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(intent="question", service=None, date_ref=explicit(next_monday())),
    ]))
    reply = engine.handle_text(CHAT, "есть время в понедельник?")
    assert slot_buttons(reply), "вопрос о наличии — всегда ответ слотами"


def test_other_with_date_continues_booking(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    # живой баг: голое «на завтра» NLU метит other+tomorrow — дата обязана
    # доехать до сценария, а не выброситься с переспросом дня
    monday = next_monday()
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(service="cleaning"),
        extr(intent="other", date_ref="tomorrow"),
    ]), clock=lambda: at_tashkent(monday, "08:00"))
    engine.handle_text(CHAT, "хочу чистку")
    reply = engine.handle_text(CHAT, "на завтра")
    assert slot_buttons(reply), "other с датой внутри записи — слоты на эту дату"
    assert fsm_state(admin_engine) == "booking_offer_slots"
    assert ctx_date(admin_engine) == (monday + timedelta(days=1)).isoformat()


def test_other_with_date_in_idle_starts_booking(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    # checkup есть в каталоге — но дефолт checkup только для question;
    # other+дата без услуги честно спрашивает услугу кнопками
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(intent="other", date_ref="tomorrow"),
    ]), clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_text(CHAT, "на завтра")
    assert any(b.action.startswith("service:") for b in reply.buttons), \
        "дата принята, сценарий начат — бот спрашивает услугу"
    assert fsm_state(admin_engine) == "booking_collect"
    assert ctx_date(admin_engine) == (monday + timedelta(days=1)).isoformat()


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


def test_general_question_not_understood_without_alert(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # П-2а: вопрос вне компетенции — «не понял» + меню, админа не дёргаем
    # (адрес научится отвечать FAQ-слой П-2б)
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="question")]),
                          notifier=notifier)
    reply = engine.handle_text(CHAT, "а где вы находитесь?")
    # полировка-2: фоллбэк отдаёт кнопку явного выхода к человеку
    assert [b.action for b in reply.buttons] == ["call_admin"]
    assert notifier.calls == []
    # вопрос без записи — не эскалация диалога, бот продолжает работать
    assert fsm_state(admin_engine) == "idle"
