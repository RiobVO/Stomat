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
