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
    p95_response_sec: float | None = None  # C-3: SLA-метрика (нет данных = None)


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
    # «язык денег» (E.1, M2): отмена ИЗ НАПОМИНАНИЯ (actor='reminder') = пациент
    # предупредил заранее вместо неявки, слот вернулся в продажу. Считаем такие
    # отмены и стоимость освобождённых слотов (цены отменённых услуг; NULL-цены
    # в сумму не входят — слот считаем, неизвестную выручку не выдумываем).
    # M2 снял прежнее требование «именно этот слот тут же перекрыт другой
    # записью» — на живом трафике оно давало ≈0 и обесценивало метрику.
    money = session.execute(
        text("""
            SELECT count(*) AS prevented, COALESCE(sum(s.price), 0) AS saved
            FROM appointment_audit aa
            JOIN appointment a ON a.id = aa.appointment_id
            LEFT JOIN service s ON s.id = a.service_id
            WHERE aa.action = 'cancel' AND aa.actor = 'reminder'
              AND (aa.at AT TIME ZONE :tz)::date = :day
        """),
        {"tz": str(tz), "day": day},
    ).one()

    # p95 ответа за локальный день: от приёма апдейта до отправленного ответа
    p95 = session.execute(
        text("""
            SELECT extract(epoch FROM percentile_cont(0.95)
                   WITHIN GROUP (ORDER BY completed_at - created_at))
            FROM message_queue
            WHERE status = 'done' AND completed_at IS NOT NULL
              AND (completed_at AT TIME ZONE :tz)::date = :day
        """),
        {"tz": str(tz), "day": day},
    ).scalar_one()

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
        p95_response_sec=round(float(p95), 1) if p95 is not None else None,
    )


def render_stats(stats: DailyStats, day: date) -> str:
    saved = f"{stats.saved_revenue:,}".replace(",", " ")
    p95_line = (f"\n• p95 ответа: {stats.p95_response_sec} с (SLA < 5 с)"
                if stats.p95_response_sec is not None else "")
    return (f"Сводка за {day:%d.%m}:\n"
            f"• записей подтверждено: {stats.booked}\n"
            f"• отмен: {stats.cancelled}\n"
            f"• предотвращено неявок: {stats.prevented_noshows} "
            f"(слотов на ≈ {saved} сум освобождено заранее)\n"
            f"• эскалаций к администратору: {stats.escalated}\n"
            f"• напоминаний доставлено: {stats.reminders_sent}\n"
            f"• LLM: {stats.llm_requests} запросов, {stats.llm_tokens} токенов, "
            f"сбоев: {stats.nlu_failures}, repair: {stats.nlu_repairs}"
            + p95_line)


QUESTIONS_IN_DIGEST = 10  # cap: дайджест — сводка, не лог


def render_questions(questions: list[str]) -> str:
    """Блок «вопросы без ответа» для дайджеста (П-2б): владелец видит
    спрос, не дёргаясь днём. Тексты уже анонимны (телефоны замаскированы)."""
    shown = questions[:QUESTIONS_IN_DIGEST]
    lines = "\n".join(f"• {q}" for q in shown)
    tail = len(questions) - len(shown)
    suffix = f"\n… и ещё {tail}" if tail > 0 else ""
    return f"❓ Вопросы без ответа ({len(questions)}):\n{lines}{suffix}"


def should_send_digest(now_local: datetime, last_digest: date | None,
                       hour: int = DIGEST_HOUR) -> bool:
    if now_local.hour < hour:
        return False
    return last_digest is None or last_digest < now_local.date()
