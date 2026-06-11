"""Копилка неотвеченных вопросов (П-2б): анонимные тексты для владельца.

Без chat_id — владельцу нужен спрос («что спрашивают»), не личности;
телефоны маскируются ДО вызова add (redact_phones). Дневная выборка идёт
в вечерний дайджест; retention чистит старше NAVBAT_RETENTION_DAYS.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session


def add(session: Session, question: str) -> None:
    session.execute(
        text("INSERT INTO unanswered_question (clinic_id, question) "
             "VALUES (current_setting('app.clinic_id')::uuid, :q)"),
        {"q": question},
    )


def for_day(session: Session, day: date, tz: str) -> list[str]:
    """Вопросы за локальный день клиники, в порядке поступления."""
    return list(session.execute(
        text("SELECT question FROM unanswered_question "
             "WHERE (at AT TIME ZONE :tz)::date = :day ORDER BY id"),
        {"tz": tz, "day": day},
    ).scalars())
