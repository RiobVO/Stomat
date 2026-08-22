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

_HAS_UPCOMING_VISIT = (
    "EXISTS (SELECT 1 FROM appointment a WHERE a.tg_chat_id = c.tg_chat_id "
    "        AND a.status = 'booked' AND lower(a.time_range) > now()) ")


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
        # Диалог пациента с предстоящим приёмом переживает чистку РАДИ ЯЗЫКА:
        # запись могут оформить за горизонт retention (брекеты, имплант —
        # месяцы), а conversation.context — единственное место, где хранится
        # язык чата, и без него напоминание уходит узбеку по-русски. Ради
        # одного поля нельзя хранить всё остальное (PRIVACY.md обещает, что
        # неактивный контекст не живёт дольше срока): от строки остаётся
        # ровно язык. Условие «есть что стирать» держит операцию идемпотентной
        stripped = session.execute(
            text("UPDATE conversation c "
                 "SET fsm_state = 'idle', patient_id = NULL, "
                 "    context = jsonb_strip_nulls("
                 "        jsonb_build_object('lang', c.context -> 'lang')) "
                 "WHERE c.updated_at < now() - make_interval(days => :days) "
                 "AND " + _HAS_UPCOMING_VISIT +
                 "AND (c.fsm_state <> 'idle' OR c.patient_id IS NOT NULL "
                 "     OR c.context - 'lang' <> '{}'::jsonb)"),
            {"days": days},
        ).rowcount
        dialogs = session.execute(
            text("DELETE FROM conversation c "
                 "WHERE c.updated_at < now() - make_interval(days => :days) "
                 "AND NOT " + _HAS_UPCOMING_VISIT),
            {"days": days},
        ).rowcount
        questions = session.execute(
            text("DELETE FROM unanswered_question "
                 "WHERE at < now() - make_interval(days => :days)"),
            {"days": days},
        ).rowcount
        # лист ожидания: терминальные записи — история без ценности; активные
        # (waiting/notified) не трогаем (их закрывает матчер/expire)
        waitlist = session.execute(
            text("DELETE FROM waitlist "
                 "WHERE status IN ('fulfilled', 'cancelled', 'expired') "
                 "AND created_at < now() - make_interval(days => :days)"),
            {"days": days},
        ).rowcount
    if messages or dialogs or questions or waitlist:
        log.info("retention: удалено %d сообщений, %d диалогов, %d вопросов, "
                 "%d записей очереди старше %d дней",
                 messages, dialogs, questions, waitlist, days)
    return messages, dialogs
