"""Доступ к настройкам клиники (таблица clinic) и календарю выходных
(holiday) — тонкий слой данных, чтобы FSM не держал сырой SQL. Функции
работают внутри tenant_transaction (RLS по clinic_id); clinic-запросы
бьют по текущему тенанту через current_setting('app.clinic_id')."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session


def clinic_name(session: Session) -> str:
    return session.execute(
        text("SELECT name FROM clinic "
             "WHERE id = current_setting('app.clinic_id')::uuid")
    ).scalar_one()


def clinic_timezone(session: Session) -> str:
    """IANA-таймзона клиники (строка, напр. 'Asia/Tashkent')."""
    return session.execute(
        text("SELECT timezone FROM clinic "
             "WHERE id = current_setting('app.clinic_id')::uuid")
    ).scalar_one()


def clinic_address(session: Session) -> str | None:
    """Адрес клиники для FAQ-ответа; NULL — не задан (онбординг --address)."""
    return session.execute(
        text("SELECT address FROM clinic "
             "WHERE id = current_setting('app.clinic_id')::uuid")
    ).scalar_one()


def holidays_on(session: Session, day: date) -> set[date]:
    """Выходные/праздники клиники, попадающие на day (пусто = рабочий)."""
    return set(session.execute(
        text("SELECT date FROM holiday WHERE date = :day"), {"day": day}
    ).scalars())
