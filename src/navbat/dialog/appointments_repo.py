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


def active_by_id(session: Session, appointment_id: str,
                 chat_id: int) -> Row | None:
    """Активная (hold/booked) БУДУЩАЯ запись чата по id — для отмены из
    напоминания и переноса по кнопке альтернативы, где запись известна по id.

    Условия те же, что у active_by_chat, и по тем же причинам: id приходит
    из callback_data, то есть от клиента, — чужой id внутри клиники отменял
    бы чужую запись (RLS изолирует клиники, но не пациентов). Начавшийся
    приём отменять нечего: слот в прошлом никому не достанется, а отмена из
    напоминания идёт в сводку владельца как предотвращённая неявка с суммой.
    """
    return session.execute(
        text("SELECT id, lower(time_range) AS start, doctor_id, service_id "
             "FROM appointment "
             "WHERE id = CAST(:id AS uuid) AND tg_chat_id = :chat "
             "AND status IN ('hold', 'booked') AND lower(time_range) > now()"),
        {"id": appointment_id, "chat": chat_id},
    ).one_or_none()


def confirm_attendance(session: Session, appointment_id: str,
                       tg_chat_id: int) -> bool:
    """Отметить «пациент придёт» по кнопке напоминания; False — отмечать нечего.

    Субъект приходит в самой кнопке (она висит в чате до приёма), поэтому
    проверяется здесь же: callback_data — вход от клиента, и чужой id внутри
    той же клиники ставил бы отметку на чужую запись (RLS изолирует клиники,
    но не пациентов). Не-booked запись (отменённая, протухший hold) — тихий
    отказ: подтверждать нечего. Повторный тап просто освежает отметку —
    пациент подтвердил то же самое.
    """
    return session.execute(
        text("UPDATE appointment "
             "SET confirm_status = 'confirmed', confirmed_at = now() "
             "WHERE id = CAST(:id AS uuid) AND tg_chat_id = :chat "
             "AND status = 'booked'"),
        {"id": appointment_id, "chat": tg_chat_id},
    ).rowcount > 0


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
