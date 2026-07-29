"""Доступ к записям (таблица appointment) из диалогового слоя — тонкий
слой данных, чтобы FSM не держал сырой SQL. Здесь только запросы, которые
нужны диалогу (поиск/привязка/guard); жизненный цикл слота (hold/confirm/
cancel) остаётся за scheduling.engine. Функции работают внутри
tenant_transaction (RLS по clinic_id)."""
from __future__ import annotations

import uuid

from sqlalchemy import Row
from sqlalchemy import text
from sqlalchemy.orm import Session


def active_by_chat(session: Session, chat_id: int) -> Row | None:
    """Ближайшая будущая активная (hold/booked) запись чата — (id, doctor_id,
    service_id, start); для переноса/отмены."""
    return session.execute(
        text("SELECT id, doctor_id, service_id, lower(time_range) AS start "
             "FROM appointment "
             "WHERE tg_chat_id = :chat AND status IN ('hold', 'booked') "
             "AND lower(time_range) > now() "
             "ORDER BY lower(time_range) LIMIT 1"),
        {"chat": chat_id},
    ).one_or_none()


def active_by_id(session: Session, appointment_id: str) -> Row | None:
    """Активная (hold/booked) запись по id — (id, start); для отмены из
    напоминания, где запись известна по id."""
    return session.execute(
        text("SELECT id, lower(time_range) AS start FROM appointment "
             "WHERE id = CAST(:id AS uuid) AND status IN ('hold', 'booked')"),
        {"id": appointment_id},
    ).one_or_none()


def slot_bounds(session: Session, appointment_id: uuid.UUID) -> Row:
    """(doctor_id, start, finish) записи — для guard-проверки, что слот ещё
    свободен в календаре перед confirm."""
    return session.execute(
        text("SELECT doctor_id, lower(time_range) AS start, "
             "upper(time_range) AS finish FROM appointment WHERE id = :id"),
        {"id": appointment_id},
    ).one()


def set_patient(session: Session, appointment_id: uuid.UUID,
                patient_id: uuid.UUID) -> None:
    """Привязать пациента к записи (после confirm, отдельной транзакцией)."""
    session.execute(
        text("UPDATE appointment SET patient_id = :p WHERE id = :a"),
        {"p": patient_id, "a": appointment_id},
    )
