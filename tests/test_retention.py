"""Retention: чистка диалогов и очереди старше 90 дней (Ф1.5, D.3).

PII-минимизация: сырые сообщения (message_queue.payload) и контексты
диалогов не должны жить вечно. pending-сообщения не трогаем — их подберёт
reclaim; записи/аудит — операционная история клиники, не чистятся.
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import Conversation, save_conversation
from navbat.reminders import ReminderService
from navbat.retention import cleanup_old_data
from navbat.telegram.queue import enqueue

CHAT_OLD, CHAT_NEW = 900, 901


def seed(app_session_factory, admin_engine, clinic_a) -> None:
    with tenant_transaction(app_session_factory, clinic_a) as session:
        enqueue(session, 1, CHAT_OLD, {"update_id": 1})   # старое done — удалить
        enqueue(session, 2, CHAT_NEW, {"update_id": 2})   # свежее done — оставить
        enqueue(session, 3, CHAT_OLD, {"update_id": 3})   # старое pending — оставить
        save_conversation(session, Conversation(chat_id=CHAT_OLD))
        save_conversation(session, Conversation(chat_id=CHAT_NEW))
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE message_queue SET status = 'done' WHERE update_id IN (1, 2)"))
        conn.execute(text(
            "UPDATE message_queue SET created_at = now() - interval '100 days' "
            "WHERE update_id IN (1, 3)"))
        conn.execute(text(
            "UPDATE conversation SET updated_at = now() - interval '100 days' "
            "WHERE tg_chat_id = :c"), {"c": CHAT_OLD})


def test_cleanup_removes_only_old_finished_data(app_session_factory,
                                                admin_engine, clinic_a):
    seed(app_session_factory, admin_engine, clinic_a)

    deleted_messages, deleted_dialogs = cleanup_old_data(app_session_factory,
                                                         clinic_a)

    assert (deleted_messages, deleted_dialogs) == (1, 1)
    with admin_engine.begin() as conn:
        left_updates = conn.execute(text(
            "SELECT update_id FROM message_queue ORDER BY update_id")).scalars().all()
        left_chats = conn.execute(text(
            "SELECT tg_chat_id FROM conversation")).scalars().all()
    assert left_updates == [2, 3], "свежее done и старое pending остались"
    assert left_chats == [CHAT_NEW]


def test_maybe_cleanup_runs_once_a_day(app_session_factory, admin_engine,
                                       clinic_a):
    seed(app_session_factory, admin_engine, clinic_a)
    service = ReminderService(app_session_factory, clinic_a)

    assert service.maybe_cleanup() is True, "первый вызов за день чистит"
    assert service.maybe_cleanup() is False, "повтор в тот же день — no-op"


def test_cleanup_keeps_dialog_of_a_patient_with_a_future_appointment(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Диалог пациента с предстоящим приёмом — не мёртвый лид.

    Запись за горизонт retention (брекеты, имплант — месяцы) плюс 90 дней
    молчания сносили диалог, а с ним ЕДИНСТВЕННОЕ место, где хранится язык
    чата: напоминание о приёме приходило узбеку по-русски."""
    from datetime import timedelta

    from conftest import at_tashkent, next_monday
    from navbat.dialog.conversation import get_chat_lang
    from navbat.scheduling.engine import SchedulingEngine

    chat = 902
    far_future = next_monday() + timedelta(days=120)
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appointment = sched.hold(doctor_a, service_cleaning,
                             at_tashkent(far_future, "09:00"), tg_chat_id=chat)
    sched.confirm(appointment)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = Conversation(chat_id=chat)
        conv.context.lang = "uz"
        save_conversation(session, conv)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE conversation SET updated_at = now() - interval '100 days' "
            "WHERE tg_chat_id = :c"), {"c": chat})

    cleanup_old_data(app_session_factory, clinic_a)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert get_chat_lang(session, chat) == "uz", \
            "язык пациента с предстоящим приёмом потерян вместе с диалогом"


def test_cleanup_strips_the_kept_dialog_down_to_its_language(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Диалог с предстоящим приёмом переживает чистку РАДИ ЯЗЫКА — и только.

    PRIVACY.md обещает, что неактивный контекст не живёт дольше 90 дней;
    исключение ради одного поля не даёт права хранить всё остальное —
    брошенный сценарий с услугой и именем обязан уйти."""
    from datetime import timedelta

    from conftest import at_tashkent, next_monday
    from navbat.dialog.conversation import get_chat_lang
    from navbat.scheduling.engine import SchedulingEngine

    chat = 903
    far_future = next_monday() + timedelta(days=120)
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appointment = sched.hold(doctor_a, service_cleaning,
                             at_tashkent(far_future, "09:00"), tg_chat_id=chat)
    sched.confirm(appointment)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = Conversation(chat_id=chat, state="awaiting_name")
        conv.context.lang = "uz"
        conv.context.service = "implant"
        conv.context.pending_name = "Азиз Рахимов"
        save_conversation(session, conv)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE conversation SET updated_at = now() - interval '100 days' "
            "WHERE tg_chat_id = :c"), {"c": chat})

    cleanup_old_data(app_session_factory, clinic_a)

    with admin_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT fsm_state, context, patient_id FROM conversation "
            "WHERE tg_chat_id = :c"), {"c": chat}).one()
    assert row.context == {"lang": "uz"}, "от диалога осталось больше языка"
    assert row.fsm_state == "idle" and row.patient_id is None
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert get_chat_lang(session, chat) == "uz"
