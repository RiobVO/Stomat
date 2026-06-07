"""Команда /forget <chat_id>: анонимизация пациента по запросу (Ф1.5, D.2).

Имя/контакт стираются, диалог и сырые сообщения удаляются; история приёмов
клиники остаётся обезличенной. Будущие записи НЕ отменяются автоматически —
запрос на удаление данных != отмена приёма.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import next_monday
from navbat.db.base import tenant_transaction
from navbat.dialog.patients import create_patient
from test_dialog_booking import CHAT, explicit, extr
from test_dialog_reschedule_cancel import book_directly
from test_tg_release import ADMIN_CHAT, sent_to
from test_tg_worker import make_worker, put_message


def test_forget_anonymizes_patient_and_wipes_dialog(app_session_factory,
                                                    admin_engine, clinic_a,
                                                    doctor_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        create_patient(session, CHAT, "Алишер", "901234567")
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  next_monday(), "09:00")
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO reminder (clinic_id, appointment_id, kind, send_at) "
            "SELECT clinic_id, id, '120m', now() + interval '1 day' "
            "FROM appointment"))
    worker, api, _ = make_worker(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(next_monday()))],
        admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "хочу чистку в понедельник")
    worker.process_one()  # conversation с контекстом + done-сообщение в очереди

    put_message(app_session_factory, clinic_a, f"/forget {CHAT}",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    with admin_engine.begin() as conn:
        patient = conn.execute(text(
            "SELECT name_encrypted, contact_hash, tg_chat_id FROM patient")).one()
        conv_count = conn.execute(
            text("SELECT count(*) FROM conversation WHERE tg_chat_id = :c"),
            {"c": CHAT}).scalar_one()
        msg_count = conn.execute(
            text("SELECT count(*) FROM message_queue WHERE tg_chat_id = :c"),
            {"c": CHAT}).scalar_one()
        appointment = conn.execute(
            text("SELECT tg_chat_id, status FROM appointment")).one()
        reminder_status = conn.execute(
            text("SELECT status FROM reminder")).scalar_one()

    assert (patient.name_encrypted, patient.contact_hash,
            patient.tg_chat_id) == (None, None, None)
    assert conv_count == 0 and msg_count == 0
    assert appointment.tg_chat_id is None and appointment.status == "booked", \
        "история приёма жива, но обезличена"
    assert reminder_status == "cancelled", "напоминание не уйдёт в стёртый чат"
    assert "[OK]" in sent_to(api, ADMIN_CHAT)[-1][0]


def test_forget_bad_argument_shows_usage(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/forget", chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/forget abc", chat_id=ADMIN_CHAT)
    worker.process_one()
    worker.process_one()

    replies = [reply for reply, _ in sent_to(api, ADMIN_CHAT)]
    assert len(replies) == 2
    for reply in replies:
        assert "/forget <chat_id>" in reply


def test_forget_unknown_chat(app_session_factory, admin_engine, clinic_a,
                             doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/forget 424242",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    assert "не найден" in sent_to(api, ADMIN_CHAT)[-1][0]


def test_forget_from_patient_goes_to_nlu(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(next_monday()))],
        admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/forget 100", chat_id=CHAT)
    worker.process_one()

    assert api.sent[0][0] == CHAT, "пациентский текст ушёл обычным путём"
