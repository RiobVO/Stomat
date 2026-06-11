"""Слой данных листа ожидания (waitlist) — тонкий, как questions_repo.

Цель очереди — «любой ближайший слот» для услуги (по любому врачу). Все
функции работают ВНУТРИ переданной session (tenant_transaction открывает
вызывающий, RLS по clinic_id). Активные статусы — waiting/notified;
терминальные — fulfilled/cancelled/expired.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

_ACTIVE = "('waiting', 'notified')"


def add(session: Session, service_id: uuid.UUID, tg_chat_id: int,
        patient_id: uuid.UUID | None, lang: str) -> int | None:
    """Поставить в очередь; None — уже есть активная запись на эту услугу
    (частичный UNIQUE ux_waitlist_active ловит дубль)."""
    return session.execute(
        text("INSERT INTO waitlist "
             "(clinic_id, service_id, tg_chat_id, patient_id, lang) "
             "VALUES (current_setting('app.clinic_id')::uuid, :sid, :chat, "
             "        CAST(:pid AS uuid), :lang) "
             "ON CONFLICT (clinic_id, tg_chat_id, service_id) "
             f"WHERE status IN {_ACTIVE} DO NOTHING RETURNING id"),
        {"sid": service_id, "chat": tg_chat_id,
         "pid": str(patient_id) if patient_id else None, "lang": lang},
    ).scalar_one_or_none()


def active_for_chat_service(session: Session, tg_chat_id: int,
                            service_id: uuid.UUID) -> Row | None:
    return session.execute(
        text(f"SELECT id, status FROM waitlist WHERE tg_chat_id = :chat "
             f"AND service_id = :sid AND status IN {_ACTIVE}"),
        {"chat": tg_chat_id, "sid": service_id},
    ).one_or_none()


def list_waiting(session: Session) -> list[Row]:
    """Активные записи, oldest-first — для матчера."""
    return list(session.execute(
        text("SELECT id, service_id, tg_chat_id, patient_id, lang, status, "
             "notified_at FROM waitlist "
             f"WHERE status IN {_ACTIVE} ORDER BY created_at, id")
    ).all())


def count_waiting(session: Session) -> int:
    return session.execute(
        text(f"SELECT count(*) FROM waitlist WHERE status IN {_ACTIVE}")
    ).scalar_one()


def mark_notified(session: Session, waitlist_id: int) -> None:
    session.execute(
        text("UPDATE waitlist SET status = 'notified', notified_at = now() "
             "WHERE id = :id"), {"id": waitlist_id})


def mark_fulfilled(session: Session, waitlist_id: int) -> None:
    session.execute(text("UPDATE waitlist SET status = 'fulfilled' "
                         "WHERE id = :id"), {"id": waitlist_id})


def mark_cancelled(session: Session, waitlist_id: int) -> None:
    session.execute(text("UPDATE waitlist SET status = 'cancelled' "
                         "WHERE id = :id"), {"id": waitlist_id})


def expire_old(session: Session, ttl_days: int) -> int:
    """Активные записи старше TTL → expired; число затронутых."""
    return session.execute(
        text(f"UPDATE waitlist SET status = 'expired' WHERE status IN {_ACTIVE} "
             "AND created_at < now() - make_interval(days => :ttl)"),
        {"ttl": ttl_days},
    ).rowcount


def has_future_booked(session: Session, tg_chat_id: int,
                      service_id: uuid.UUID) -> bool:
    """Есть ли будущая подтверждённая запись этого чата на услугу —
    авто-fulfillment (пациент записался, в т.ч. по нашему пушу)."""
    return bool(session.execute(
        text("SELECT EXISTS (SELECT 1 FROM appointment "
             "WHERE tg_chat_id = :chat AND service_id = :sid "
             "AND status = 'booked' AND lower(time_range) > now())"),
        {"chat": tg_chat_id, "sid": service_id},
    ).scalar_one())
