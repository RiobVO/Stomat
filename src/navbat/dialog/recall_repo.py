"""Журнал recall-приглашений (recall_outreach) из диалогового слоя — тонкий
слой данных, как questions_repo: в сценариях нет сырого SQL. Рассылка живёт
в reminders (фон), сюда приходит только конверсия — отметка «пациент вернулся
по этому приглашению». Функции работают ВНУТРИ переданной session
(tenant_transaction открывает вызывающий, RLS по clinic_id).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def mark_booked(session: Session, appointment_id: str) -> bool:
    """Отметить возврат по приглашению; False — отмечать нечего.

    `booked_at IS NULL` в условии: конверсия засчитывается ОДИН раз, первой
    записью. Кнопка приглашения живёт в чате и переживает эту запись — второй
    тап и вторая запись иначе сдвигали бы дату конверсии в чужой период
    витрины. False — строку уже отметили или её вычистил ретеншен: это не
    ошибка, пациент всё равно записан.
    """
    return session.execute(
        text("UPDATE recall_outreach SET booked_at = now() "
             "WHERE appointment_id = CAST(:src AS uuid) AND booked_at IS NULL"),
        {"src": appointment_id},
    ).rowcount > 0
