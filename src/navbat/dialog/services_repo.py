"""Доступ к каталогу услуг клиники (таблица service) — тонкий слой
данных, чтобы FSM не держал сырой SQL. Все функции работают внутри
tenant_transaction (RLS по clinic_id). Имена услуг — канонические ключи
из navbat.nlu.schema.SERVICE_KEYS; метки для показа — в replies."""
from __future__ import annotations

import uuid

from sqlalchemy import Row
from sqlalchemy import text
from sqlalchemy.orm import Session


def service_id(session: Session, key: str) -> uuid.UUID | None:
    return session.execute(
        text("SELECT id FROM service WHERE name = :name ORDER BY name LIMIT 1"),
        {"name": key},
    ).scalar_one_or_none()


def service_name(session: Session, sid: uuid.UUID) -> str | None:
    return session.execute(
        text("SELECT name FROM service WHERE id = :id"), {"id": sid}
    ).scalar_one_or_none()


def service_keys(session: Session) -> list[str]:
    """Ключи всех услуг клиники, по алфавиту (для кнопок выбора)."""
    return list(session.execute(
        text("SELECT name FROM service ORDER BY name")
    ).scalars().all())


def price_list(session: Session) -> list[Row]:
    """(name, price) всех услуг по алфавиту; price может быть NULL."""
    return list(session.execute(
        text("SELECT name, price FROM service ORDER BY name")).all())


def service_price(session: Session, key: str) -> int | None:
    return session.execute(
        text("SELECT price FROM service WHERE name = :name LIMIT 1"),
        {"name": key},
    ).scalar_one_or_none()
