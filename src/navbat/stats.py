"""Сводка для владельца клиники: ценность сверху, техника внизу (П-6).

/stats — день, /stats 7|30 — период; вечерний дайджест — тот же рендер
за день. Владелец читает деньги (записи, предотвращённые неявки, записи
вне рабочих часов), не токены. Сводка только на русском: адресат —
владелец клиники, не пациент.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.scheduling.calendar_rules import open_bounds

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
    after_hours_booked: int = 0  # П-6: «бот записал, пока клиника спала»


def collect_daily_stats(session: Session, day: date, tz: ZoneInfo) -> DailyStats:
    """Цифры за локальный день клиники (дайджест и /stats без аргумента)."""
    return collect_stats(session, day, day, tz)


def collect_stats(session: Session, first: date, last: date,
                  tz: ZoneInfo) -> DailyStats:
    """Цифры за период [first, last] локальных дней клиники (П-6)."""
    span = {"tz": str(tz), "first": first, "last": last}

    def audit_count(action: str) -> int:
        return session.execute(
            text("SELECT count(*) FROM appointment_audit "
                 "WHERE action = :action "
                 "AND (at AT TIME ZONE :tz)::date BETWEEN :first AND :last"),
            {"action": action, **span},
        ).scalar_one()

    escalated = session.execute(
        text("SELECT count(*) FROM conversation WHERE fsm_state = 'escalated' "
             "AND (updated_at AT TIME ZONE :tz)::date BETWEEN :first AND :last"),
        span,
    ).scalar_one()
    reminders_sent = session.execute(
        text("SELECT count(*) FROM reminder WHERE status = 'sent' "
             "AND (sent_at AT TIME ZONE :tz)::date BETWEEN :first AND :last"),
        span,
    ).scalar_one()
    llm = session.execute(
        text("SELECT COALESCE(sum(requests), 0) AS requests, "
             "COALESCE(sum(in_tokens + out_tokens), 0) AS tokens, "
             "COALESCE(sum(failures), 0) AS failures, "
             "COALESCE(sum(repairs), 0) AS repairs "
             "FROM llm_usage WHERE day BETWEEN :first AND :last"),
        {"first": first, "last": last},
    ).one()
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
              AND (aa.at AT TIME ZONE :tz)::date BETWEEN :first AND :last
        """),
        span,
    ).one()

    # p95 ответа за период: от приёма апдейта до отправленного ответа
    p95 = session.execute(
        text("""
            SELECT extract(epoch FROM percentile_cont(0.95)
                   WITHIN GROUP (ORDER BY completed_at - created_at))
            FROM message_queue
            WHERE status = 'done' AND completed_at IS NOT NULL
              AND (completed_at AT TIME ZONE :tz)::date BETWEEN :first AND :last
        """),
        span,
    ).scalar_one()

    return DailyStats(
        booked=audit_count("confirm"),
        cancelled=audit_count("cancel"),
        escalated=escalated,
        reminders_sent=reminders_sent,
        llm_requests=int(llm.requests),
        llm_tokens=int(llm.tokens),
        nlu_failures=int(llm.failures),
        nlu_repairs=int(llm.repairs),
        prevented_noshows=money.prevented,
        saved_revenue=int(money.saved),
        p95_response_sec=round(float(p95), 1) if p95 is not None else None,
        after_hours_booked=_after_hours_confirms(session, first, last, tz),
    )


def _after_hours_confirms(session: Session, first: date, last: date,
                          tz: ZoneInfo) -> int:
    """Подтверждения вне рабочего окна своего дня (П-6): главный аргумент
    продажи — бот записывает, когда администратор спит. День целиком
    закрыт (выходной/праздник) — тоже «вне часов». Объёмы малы — считаем
    кодом по строкам аудита."""
    moments = session.execute(
        text("SELECT at FROM appointment_audit WHERE action = 'confirm' "
             "AND (at AT TIME ZONE :tz)::date BETWEEN :first AND :last"),
        {"tz": str(tz), "first": first, "last": last},
    ).scalars().all()
    if not moments:
        return 0
    schedules = session.execute(
        text("SELECT working_intervals FROM doctor")).scalars().all()
    holidays = set(session.execute(
        text("SELECT date FROM holiday WHERE date BETWEEN :first AND :last"),
        {"first": first, "last": last},
    ).scalars())
    count = 0
    for moment in moments:
        day = moment.astimezone(tz).date()
        bounds = open_bounds(schedules, day, tz,
                             {day} if day in holidays else set())
        if bounds is None:
            count += 1
            continue
        lo, hi = bounds
        if not (lo <= moment < hi):
            count += 1
    return count


def render_stats(stats: DailyStats, day: date, last: date | None = None) -> str:
    """Рендер владельца (П-6): ценность сверху, техника одной строкой внизу."""
    if last is None or last == day:
        header = f"📊 Сводка за {day:%d.%m}:"
    else:
        days = (last - day).days + 1
        header = f"📊 Сводка за {days} дн. ({day:%d.%m}–{last:%d.%m}):"
    saved = f"{stats.saved_revenue:,}".replace(",", " ")
    after = (f" (из них {stats.after_hours_booked} — вне рабочих часов)"
             if stats.after_hours_booked else "")
    p95_part = (f" · p95 ответа: {stats.p95_response_sec} с (SLA < 5 с)"
                if stats.p95_response_sec is not None else "")
    return (f"{header}\n"
            f"💰 Ценность\n"
            f"• записей подтверждено: {stats.booked}{after}\n"
            f"• предотвращено неявок: {stats.prevented_noshows} "
            f"(слотов на ≈ {saved} сум освобождено заранее)\n"
            f"• отмен: {stats.cancelled}\n"
            f"• эскалаций к администратору: {stats.escalated}\n"
            f"⚙️ Служебное\n"
            f"• напоминаний: {stats.reminders_sent} · LLM: {stats.llm_requests} "
            f"запросов, {stats.llm_tokens} токенов, "
            f"сбоев: {stats.nlu_failures}, repair: {stats.nlu_repairs}"
            + p95_part)


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
