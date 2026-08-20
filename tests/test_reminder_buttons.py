"""Кнопки напоминания: «Приду» — подтверждение, «Отменить» — полный cancel-поток."""
from __future__ import annotations

from sqlalchemy import text

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import CHAT, fsm_state
from test_gcal_export import book


def make_engine(app_session_factory, clinic_id) -> DialogEngine:
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=[]))


def test_attend_button_confirms_without_state_change(app_session_factory,
                                                     admin_engine, clinic_a,
                                                     doctor_a, service_cleaning):
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00", chat_id=CHAT)
    engine = make_engine(app_session_factory, clinic_a)
    reply = engine.handle_action(CHAT, f"attend:{appointment_id}")

    assert reply.text == TEMPLATES["attend_ok"]["ru"]
    assert not reply.buttons
    assert fsm_state(admin_engine) == "idle"


def test_attend_button_works_in_escalated(app_session_factory, admin_engine,
                                          clinic_a, doctor_a, service_cleaning):
    # живой тест 12.06: пациент позвал администратора (escalated), потом
    # пришло напоминание — тап «Приду» отвечал «передаю администратору».
    # Подтверждение не меняет состояние и обязано работать в заморозке
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00", chat_id=CHAT)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO conversation (clinic_id, tg_chat_id, fsm_state) "
            "VALUES (:cl, :c, 'escalated')"), {"cl": clinic_a, "c": CHAT})
    engine = make_engine(app_session_factory, clinic_a)
    reply = engine.handle_action(CHAT, f"attend:{appointment_id}")

    assert reply.text == TEMPLATES["attend_ok"]["ru"]
    assert fsm_state(admin_engine) == "escalated", \
        "заморозка не снимается — диалог по-прежнему у администратора"


def test_remind_cancel_runs_full_cancel_flow(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00", chat_id=CHAT)
    engine = make_engine(app_session_factory, clinic_a)

    confirm = engine.handle_action(CHAT, f"remind_cancel:{appointment_id}")
    assert {b.action for b in confirm.buttons} == {"cancel_yes", "cancel_no"}
    assert fsm_state(admin_engine) == "cancel_confirm"

    engine.handle_action(CHAT, "cancel_yes")
    with admin_engine.begin() as conn:
        status = conn.execute(text("SELECT status FROM appointment")).scalar_one()
    assert status == "cancelled", "отмена из напоминания освобождает слот"
    assert fsm_state(admin_engine) == "idle"


def test_remind_cancel_touches_only_own_appointment(app_session_factory,
                                                    admin_engine, clinic_a,
                                                    doctor_a, service_cleaning):
    """Кнопка напоминания несёт id записи — значит, id надо проверять.

    callback_data приходит от клиента, и запись ищется по одному id: чужой
    id внутри той же клиники отменил бы чужую запись (RLS изолирует клиники,
    но не пациентов между собой)."""
    victim_chat = CHAT + 1
    victim_id, _ = book(app_session_factory, clinic_a, doctor_a,
                        service_cleaning, next_monday(), "11:00",
                        chat_id=victim_chat)
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"remind_cancel:{victim_id}")
    assert not reply.buttons, "бот предложил отменить чужую запись"
    assert fsm_state(admin_engine, CHAT) == "idle"
    with admin_engine.begin() as conn:
        status = conn.execute(text("SELECT status FROM appointment "
                                   "WHERE id = :id"), {"id": victim_id}).scalar_one()
    assert status == "booked"


def test_remind_cancel_after_the_visit_started(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    """Напоминание за 2 часа висит в чате и после начала приёма.

    Тап по «Отменить запись» через минуту после начала — это неявка, а не
    предотвращённая неявка: освобождать прошедший слот не для кого, а отмена
    из напоминания идёт в сводку владельца деньгами."""
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00",
                             chat_id=CHAT)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE appointment SET time_range = tstzrange("
            "now() - interval '1 minute', now() + interval '29 minutes', '[)')"
            " WHERE id = :id"), {"id": appointment_id})
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"remind_cancel:{appointment_id}")
    assert not reply.buttons, "бот предложил отменить начавшийся приём"
    with admin_engine.begin() as conn:
        status = conn.execute(text("SELECT status FROM appointment "
                                   "WHERE id = :id"), {"id": appointment_id}).scalar_one()
        reminder_cancels = conn.execute(text(
            "SELECT count(*) FROM appointment_audit "
            "WHERE action = 'cancel' AND actor = 'reminder'")).scalar_one()
    assert status == "booked"
    assert reminder_cancels == 0, \
        "состоявшийся приём попал в сводку как предотвращённая неявка"


def test_remind_cancel_for_already_cancelled(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, next_monday(), "09:00",
                                 chat_id=CHAT)
    sched.cancel(appointment_id)
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"remind_cancel:{appointment_id}")
    assert not reply.buttons
    assert fsm_state(admin_engine) == "idle"
