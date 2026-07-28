"""Доступ к данным врачей (таблица doctor) — тонкий слой данных, чтобы
FSM не держал сырой SQL. Имена врачей зашифрованы (navbat.crypto) и
дешифруются здесь, как в patients.find_patient_by_chat. Функции работают
внутри tenant_transaction (RLS по clinic_id)."""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.crypto import decrypt_text


def working_intervals(session: Session) -> list:
    """working_intervals всех врачей клиники (JSON-графики для open_bounds)."""
    return list(session.execute(
        text("SELECT working_intervals FROM doctor")).scalars().all())


def doctor_list(session: Session) -> list[tuple[uuid.UUID, str | None]]:
    """(id, расшифрованное имя | None) всех врачей клиники, по id."""
    rows = session.execute(
        text("SELECT id, name_encrypted FROM doctor ORDER BY id")).all()
    return [
        (row.id, decrypt_text(row.name_encrypted) if row.name_encrypted else None)
        for row in rows
    ]
