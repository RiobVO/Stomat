"""Лайт-сводка дня для админа: цифры из аудита и учёта токенов.

Полный дашборд с деньгами (предотвращённые неявки, сохранённая выручка) — Ф2;
здесь — то, что продаёт идею на демо: бот работает и считает себя сам.
Сводка только на русском: адресат — владелец клиники, не пациент.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

DIGEST_HOUR = 21  # локальный час отправки вечерней сводки


@dataclass(frozen=True)
class DailyStats:
    booked: int
    cancelled: int
    escalated: int
    reminders_sent: int
    llm_requests: int
    llm_tokens: int
    nlu_failures: int
    nlu_repairs: int
    prevented_noshows: int
    saved_revenue: int


def collect_daily_stats(session: Session, day: date, tz: ZoneInfo) -> DailyStats:
    """Цифры за локальный день клиники (границы суток — в её таймзоне)."""
    def audit_count(action: str) -> int:
        return session.execute(
            text("SELECT count(*) FROM appointment_audit "
                 "WHERE action = :action "
                 "AND (at AT TIME ZONE :tz)::date = :day"),
            {"action": action, "tz": str(tz), "day": day},
        ).scalar_one()

    escalated = session.execute(
        text("SELECT count(*) FROM conversation WHERE fsm_state = 'escalated' "
             "AND (updated_at AT TIME ZONE :tz)::date = :day"),
        {"tz": str(tz), "day": day},
    ).scalar_one()
    reminders_sent = session.execute(
        text("SELECT count(*) FROM reminder WHERE status = 'sent' "
             "AND (sent_at AT TIME ZONE :tz)::date = :day"),
        {"tz": str(tz), "day": day},
    ).scalar_one()
    llm = session.execute(
        text("SELECT requests, in_tokens + out_tokens AS tokens, "
             "failures, repairs FROM llm_usage WHERE day = :day"),
        {"day": day},
    ).one_or_none()
    # «язык денег» (E.1): отмена ИЗ НАПОМИНАНИЯ (actor='reminder'), чей слот
    # потом перезаписан другой booked-записью того же врача; сохранённая
    # выручка — цены новых записей (NULL-цены в сумму не входят)
    money = session.execute(
        text("""
            WITH freed AS (
                SELECT a1.id AS old_id, a1.doctor_id, a1.time_range, aa.at
                FROM appointment_audit aa
                JOIN appointment a1 ON a1.id = aa.appointment_id
                WHERE aa.action = 'cancel' AND aa.actor = 'reminder'
                  AND (aa.at AT TIME ZONE :tz)::date = :day
            ), resold AS (
                SELECT DISTINCT ON (a2.id) a2.id, f.old_id, s.price
                FROM freed f
                JOIN appointment a2 ON a2.doctor_id = f.doctor_id
                    AND a2.status = 'booked' AND a2.id != f.old_id
                    AND a2.time_range && f.time_range
                    AND a2.created_at > f.at
                LEFT JOIN service s ON s.id = a2.service_id
            )
            SELECT count(DISTINCT old_id) AS prevented,
                   COALESCE(sum(price), 0) AS saved
            FROM resold
        """),
        {"tz": str(tz), "day": day},
    ).one()

    return DailyStats(
        booked=audit_count("confirm"),
        cancelled=audit_count("cancel"),
        escalated=escalated,
        reminders_sent=reminders_sent,
        llm_requests=llm.requests if llm else 0,
        llm_tokens=llm.tokens if llm else 0,
        nlu_failures=llm.failures if llm else 0,
        nlu_repairs=llm.repairs if llm else 0,
        prevented_noshows=money.prevented,
        saved_revenue=int(money.saved),
    )


def render_stats(stats: DailyStats, day: date) -> str:
    saved = f"{stats.saved_revenue:,}".replace(",", " ")
    return (f"Сводка за {day:%d.%m}:\n"
            f"• записей подтверждено: {stats.booked}\n"
            f"• отмен: {stats.cancelled}\n"
            f"• предотвращено неявок: {stats.prevented_noshows} "
            f"(сохранено ≈ {saved} сум)\n"
            f"• эскалаций к администратору: {stats.escalated}\n"
            f"• напоминаний доставлено: {stats.reminders_sent}\n"
            f"• LLM: {stats.llm_requests} запросов, {stats.llm_tokens} токенов, "
            f"сбоев: {stats.nlu_failures}, repair: {stats.nlu_repairs}")


def should_send_digest(now_local: datetime, last_digest: date | None,
                       hour: int = DIGEST_HOUR) -> bool:
    if now_local.hour < hour:
        return False
    return last_digest is None or last_digest < now_local.date()
