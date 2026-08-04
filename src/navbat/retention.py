"""Retention: чистка PII-носителей старше NAVBAT_RETENTION_DAYS (Ф1.5, D.3).

Чистятся сырые сообщения пациентов (message_queue.payload) в конечных
статусах и протухшие контексты диалогов (conversation) — включая escalated:
мёртвый лид старше 90 дней — это PII без операционной ценности. pending
в очереди не трогаем (подберёт reclaim); записи и аудит — операционная
история клиники, не чистятся.
"""
from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction

log = logging.getLogger("navbat.retention")

RETENTION_DAYS = int(os.environ.get("NAVBAT_RETENTION_DAYS", 90))


def cleanup_old_data(session_factory: sessionmaker[Session],
                     clinic_id: uuid.UUID,
                     days: int = RETENTION_DAYS) -> tuple[int, int]:
    """Удаляет старые done/failed-сообщения и неактивные диалоги.

    Возвращает (удалено сообщений, удалено диалогов). Идемпотентно —
    повторный запуск в тот же день безвреден.
    """
    with tenant_transaction(session_factory, clinic_id) as session:
        messages = session.execute(
            text("DELETE FROM message_queue "
                 "WHERE status IN ('done', 'failed') "
                 "AND created_at < now() - make_interval(days => :days)"),
            {"days": days},
        ).rowcount
        dialogs = session.execute(
            text("DELETE FROM conversation "
                 "WHERE updated_at < now() - make_interval(days => :days)"),
            {"days": days},
        ).rowcount
        questions = session.execute(
            text("DELETE FROM unanswered_question "
                 "WHERE at < now() - make_interval(days => :days)"),
            {"days": days},
        ).rowcount
    if messages or dialogs or questions:
        log.info("retention: удалено %d сообщений, %d диалогов, %d вопросов "
                 "старше %d дней", messages, dialogs, questions, days)
    return messages, dialogs
