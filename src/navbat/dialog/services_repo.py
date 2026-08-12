"""Доступ к каталогу услуг клиники (таблица service) — тонкий слой
данных, чтобы FSM не держал сырой SQL. Все функции работают внутри
tenant_transaction (RLS по clinic_id). Имена услуг — канонические ключи
из navbat.nlu.schema.SERVICE_KEYS; метки для показа — в replies.

Правило фильтрации is_active:
  service_id / service_keys / price_list / service_price — только активные
  (пациент не может записаться на деактивированную услугу).
  service_name — без фильтра (история записей ссылается на старые услуги).
  service_list_all — все записи для админ-консоли."""
from __future__ import annotations

import uuid

from sqlalchemy import Row
from sqlalchemy import text
from sqlalchemy.orm import Session


def service_id(session: Session, key: str) -> uuid.UUID | None:
    return session.execute(
        text("SELECT id FROM service WHERE name = :name AND is_active "
             "ORDER BY name LIMIT 1"),
        {"name": key},
    ).scalar_one_or_none()


def service_name(session: Session, sid: uuid.UUID) -> str | None:
    # Без фильтра is_active: история appointment ссылается на деактивированные услуги.
    return session.execute(
        text("SELECT name FROM service WHERE id = :id"), {"id": sid}
    ).scalar_one_or_none()


def service_keys(session: Session) -> list[str]:
    """Ключи активных услуг клиники, по алфавиту (для кнопок выбора)."""
    return list(session.execute(
        text("SELECT name FROM service WHERE is_active ORDER BY name")
    ).scalars().all())


def price_list(session: Session) -> list[Row]:
    """(name, price) активных услуг по алфавиту; price может быть NULL."""
    return list(session.execute(
        text("SELECT name, price FROM service WHERE is_active ORDER BY name")
    ).all())


def service_price(session: Session, key: str) -> int | None:
    return session.execute(
        text("SELECT price FROM service WHERE name = :name AND is_active LIMIT 1"),
        {"name": key},
    ).scalar_one_or_none()


def service_list_all(session: Session) -> list[Row]:
    """ВСЕ услуги (вкл. деактивированные) для админ-консоли:
    (name, duration_min, price, is_active), по алфавиту."""
    return list(session.execute(
        text("SELECT name, duration_min, price, is_active FROM service "
             "ORDER BY name")
    ).all())
