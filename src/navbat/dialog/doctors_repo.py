"""Доступ к данным врачей (таблица doctor) — тонкий слой данных, чтобы
FSM не держал сырой SQL. Имена врачей зашифрованы (navbat.crypto) и
дешифруются здесь, как в patients.find_patient_by_chat. Функции работают
внутри tenant_transaction (RLS по clinic_id)."""
from __future__ import annotations

import uuid
from collections import namedtuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.crypto import decrypt_text

_Doc = namedtuple(
    "Doc", "id name working_intervals buffer_min gcal_calendar_id is_active")


def working_intervals(session: Session) -> list:
    """working_intervals активных врачей клиники (для open_bounds/слотов)."""
    return list(session.execute(
        text("SELECT working_intervals FROM doctor WHERE is_active")
    ).scalars().all())


def doctor_list(session: Session) -> list[tuple[uuid.UUID, str | None]]:
    """(id, имя) активных врачей клиники, по id — для записи и слотов."""
    rows = session.execute(
        text("SELECT id, name_encrypted FROM doctor WHERE is_active ORDER BY id")
    ).all()
    return [
        (row.id, decrypt_text(row.name_encrypted) if row.name_encrypted else None)
        for row in rows
    ]


def doctor_list_all(session: Session) -> list[_Doc]:
    """ВСЕ врачи (вкл. деактивированных) для админ-консоли: namedtuple-строки
    с расшифрованным именем (id, name, working_intervals, buffer_min,
    gcal_calendar_id, is_active). Активные — первыми."""
    rows = session.execute(
        text("SELECT id, name_encrypted, working_intervals, buffer_min, "
             "gcal_calendar_id, is_active FROM doctor ORDER BY is_active DESC, id")
    ).all()
    return [
        _Doc(r.id,
             decrypt_text(r.name_encrypted) if r.name_encrypted else None,
             r.working_intervals, r.buffer_min, r.gcal_calendar_id, r.is_active)
        for r in rows
    ]
