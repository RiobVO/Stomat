"""Якоря свайп-ответов админа (admin_relay) — тонкий слой, как questions_repo.

Связь «сообщение в админ-чате → пациент»: админ отвечает реплаем на алерт, а
Bot API отдаёт только message_id сообщения, на которое ответили. Все функции
работают ВНУТРИ переданной session (tenant_transaction открывает вызывающий,
RLS по clinic_id).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def save_anchor(session: Session, admin_chat_id: int, message_id: int,
                patient_chat_id: int) -> None:
    """Запомнить, кому уйдёт реплай на это сообщение админ-чата.

    DO NOTHING, а не UPDATE: message_id уникален внутри чата, значит конфликт —
    это повторная запись того же якоря (ретрай), а не новый адресат.
    """
    session.execute(
        text("INSERT INTO admin_relay "
             "(clinic_id, admin_chat_id, message_id, patient_chat_id) "
             "VALUES (current_setting('app.clinic_id')::uuid, "
             "        :admin, :msg, :patient) "
             "ON CONFLICT (clinic_id, admin_chat_id, message_id) DO NOTHING"),
        {"admin": admin_chat_id, "msg": message_id,
         "patient": patient_chat_id},
    )


def patient_for(session: Session, admin_chat_id: int,
                message_id: int) -> int | None:
    """Пациент, которому адресован реплай. None — якоря нет (реплай на
    консольное сообщение или на алерт старше ретеншена): пересылать вслепую
    нельзя, админу уходит подсказка."""
    return session.execute(
        text("SELECT patient_chat_id FROM admin_relay "
             "WHERE admin_chat_id = :admin AND message_id = :msg"),
        {"admin": admin_chat_id, "msg": message_id},
    ).scalar_one_or_none()


def cleanup_old(session: Session, days: int = 7) -> int:
    """Якоря старше days; число удалённых. Якорь нужен, пока жива переписка по
    эскалации — недели хватает с запасом на выходные."""
    return session.execute(
        text("DELETE FROM admin_relay "
             "WHERE created_at < now() - make_interval(days => :days)"),
        {"days": days},
    ).rowcount
