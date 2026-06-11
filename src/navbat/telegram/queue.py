"""Durable-очередь Telegram-апдейтов поверх Postgres (без брокера).

Гарантии:
- идемпотентность: UNIQUE (clinic_id, update_id) + ON CONFLICT DO NOTHING —
  дубль webhook/повтор getUpdates безвреден;
- ack после обработки: клейм двухфазный (pending→processing своей транзакцией,
  done — после успеха); упавший воркер оставляет processing — его возвращает
  reclaim_stale;
- per-chat порядок: клейм отдаёт апдейт, только если в его чате нет processing
  и нет pending с меньшим update_id — сериализацию И порядок решает один
  запрос с FOR UPDATE SKIP LOCKED, разные чаты параллелятся.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.crypto import encrypt_text
from navbat.db.base import tenant_transaction
from navbat.dialog.patients import normalize_phone, phone_to_hash

MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=5)


def _redact_contact_phone(session: Session, payload: dict) -> None:
    """PII-граница очереди: открытый номер из кнопки «Поделиться контактом»
    в durable-payload не сохраняем. Телефон хэшируется здесь (соль доступна
    в tenant-транзакции) — в постоянную таблицу пациента уходит тот же хэш —
    и шифруется AES-256-GCM (пересмотр 11.06: номер нужен владельцу в
    календаре; паритет с именем). Принимается номер любой страны (П-2в).
    Нераспознаваемый контакт (мусор в цифрах) остаётся без хэша и
    шифртекста — диалог повторит кнопку контакта.
    """
    message = payload.get("message")
    if not isinstance(message, dict):
        return
    contact = message.get("contact")
    if not isinstance(contact, dict) or "phone_number" not in contact:
        return
    raw = contact.pop("phone_number")
    try:
        hashed = phone_to_hash(session, raw)
    except ValueError:
        return  # мусор в цифрах: номер уже вырезан, хэша нет → повтор кнопки
    contact["phone_hash"] = hashed
    contact["phone_encrypted"] = encrypt_text(normalize_phone(raw))


@dataclass(frozen=True)
class QueuedUpdate:
    id: int
    update_id: int
    tg_chat_id: int
    payload: dict
    attempts: int


def enqueue(session: Session, update_id: int, tg_chat_id: int, payload: dict) -> bool:
    """Кладёт апдейт; False — уже есть (дубль)."""
    _redact_contact_phone(session, payload)
    inserted = session.execute(
        text("""
            INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, payload)
            VALUES (current_setting('app.clinic_id')::uuid, :update_id, :chat,
                    CAST(:payload AS jsonb))
            ON CONFLICT (clinic_id, update_id) DO NOTHING
            RETURNING id
        """),
        {"update_id": update_id, "chat": tg_chat_id,
         "payload": json.dumps(payload, ensure_ascii=False)},
    ).scalar_one_or_none()
    return inserted is not None


def claim_next(session_factory: sessionmaker[Session],
               clinic_id: uuid.UUID) -> QueuedUpdate | None:
    """Забирает следующий апдейт в обработку. Собственная транзакция:
    статус processing должен быть виден другим воркерам сразу."""
    with tenant_transaction(session_factory, clinic_id) as session:
        row = session.execute(
            text("""
                UPDATE message_queue
                SET status = 'processing', claimed_at = now(), attempts = attempts + 1
                WHERE id = (
                    SELECT mq.id FROM message_queue mq
                    WHERE mq.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1 FROM message_queue m2
                          WHERE m2.clinic_id = mq.clinic_id
                            AND m2.tg_chat_id = mq.tg_chat_id
                            AND (m2.status = 'processing'
                                 OR (m2.status = 'pending'
                                     AND m2.update_id < mq.update_id))
                      )
                    ORDER BY mq.update_id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, update_id, tg_chat_id, payload, attempts
            """)
        ).one_or_none()
    if row is None:
        return None
    return QueuedUpdate(id=row.id, update_id=row.update_id,
                        tg_chat_id=row.tg_chat_id, payload=row.payload,
                        attempts=row.attempts)


def complete(session: Session, queue_id: int) -> None:
    session.execute(
        text("UPDATE message_queue SET status = 'done', completed_at = now() "
             "WHERE id = :id"),
        {"id": queue_id},
    )


def fail(session: Session, queue_id: int, max_attempts: int = MAX_ATTEMPTS) -> str:
    """Попытка не удалась: возврат в pending или dead letter. Возвращает новый статус."""
    return session.execute(
        text("""
            UPDATE message_queue
            SET status = CASE WHEN attempts >= :max THEN 'failed' ELSE 'pending' END
            WHERE id = :id
            RETURNING status
        """),
        {"id": queue_id, "max": max_attempts},
    ).scalar_one()


def reclaim_stale(session: Session, older_than: timedelta = STALE_AFTER) -> int:
    """Возвращает зависшие processing (умерший воркер) в pending."""
    rows = session.execute(
        text("""
            UPDATE message_queue SET status = 'pending', claimed_at = NULL
            WHERE status = 'processing' AND claimed_at < now() - :age
            RETURNING id
        """),
        {"age": older_than},
    ).scalars().all()
    return len(rows)
