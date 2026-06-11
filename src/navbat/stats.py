"""Сводка для владельца клиники: ценность сверху, техника внизу (П-6).

/stats — день, /stats 7|30 — период; вечерний дайджест — тот же рендер
за день. Владелец читает деньги (записи, предотвращённые неявки, записи
вне рабочих часов), не токены. Сводка только на русском: адресат —
владелец клиники, не пациент.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.dialog.doctors_repo import doctor_list
from navbat.dialog.replies import service_label
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
    # /stats v2 (полировка-2, В) — дефолты, чтобы не ломать конструкторы
    new_patients: int = 0        # первая не-hold запись пациента — в периоде
    returning_patients: int = 0  # были раньше И записались в периоде
    top_doctors: tuple[tuple[str, int, int], ...] = ()  # (имя, записей, сумма)
    hit_service: tuple[str, int] | None = None          # (ключ услуги, записей)


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

    # В: новые/вернувшиеся клиенты. Личность — patient_id, для записей без
    # пациента (демо, ручной онбординг) — tg_chat_id; у patient нет created_at,
    # «первый визит» считаем по appointment.created_at (без новой миграции).
    # Голый hold/expired — не визит; cancelled — визит (человек обращался).
    clients = session.execute(
        text("""
            WITH visits AS (
                SELECT COALESCE(patient_id::text, tg_chat_id::text) AS person,
                       (created_at AT TIME ZONE :tz)::date AS day
                FROM appointment
                WHERE status IN ('booked', 'done', 'cancelled')
                  AND COALESCE(patient_id::text, tg_chat_id::text) IS NOT NULL
            ),
            firsts AS (
                SELECT person, min(day) AS first_day FROM visits GROUP BY person
            )
            SELECT
                count(*) FILTER (WHERE first_day BETWEEN :first AND :last)
                    AS new_count,
                count(*) FILTER (WHERE first_day < :first) AS returning_count
            FROM firsts
            WHERE EXISTS (SELECT 1 FROM visits v
                          WHERE v.person = firsts.person
                            AND v.day BETWEEN :first AND :last)
        """),
        span,
    ).one()

    # В: топ-3 врачей по confirm-аудитам периода; NULL-цены в сумму не входят
    # (неизвестную выручку не выдумываем). Имена зашифрованы — мержим в коде.
    doctor_rows = session.execute(
        text("""
            SELECT a.doctor_id, count(*) AS cnt,
                   COALESCE(sum(s.price), 0) AS revenue
            FROM appointment_audit aa
            JOIN appointment a ON a.id = aa.appointment_id
            LEFT JOIN service s ON s.id = a.service_id
            WHERE aa.action = 'confirm'
              AND (aa.at AT TIME ZONE :tz)::date BETWEEN :first AND :last
            GROUP BY a.doctor_id
            ORDER BY cnt DESC, a.doctor_id
            LIMIT 3
        """),
        span,
    ).all()
    names = dict(doctor_list(session)) if doctor_rows else {}
    top_doctors = tuple(
        (names.get(row.doctor_id) or "Врач", row.cnt, int(row.revenue))
        for row in doctor_rows)

    # В: хит-услуга — максимум confirm'ов периода по ключу услуги
    hit = session.execute(
        text("""
            SELECT s.name, count(*) AS cnt
            FROM appointment_audit aa
            JOIN appointment a ON a.id = aa.appointment_id
            JOIN service s ON s.id = a.service_id
            WHERE aa.action = 'confirm'
              AND (aa.at AT TIME ZONE :tz)::date BETWEEN :first AND :last
            GROUP BY s.name
            ORDER BY cnt DESC, s.name
            LIMIT 1
        """),
        span,
    ).one_or_none()

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
        new_patients=clients.new_count,
        returning_patients=clients.returning_count,
        top_doctors=top_doctors,
        hit_service=(hit.name, hit.cnt) if hit else None,
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


def _money(amount: int) -> str:
    """1400000 → «1 400 000» — суммы в сум читаются с пробелами."""
    return f"{amount:,}".replace(",", " ")


def _trend(cur: int, prev: int) -> str:
    """Суффикс « ↑N%»/« ↓N%» к метрике против prev-периода (В).

    На малых числах проценты — шум («рост 100%» из 1→2 пугает владельца),
    поэтому обе выборки должны быть ≥ 10; prev=0 покрывается тем же порогом.
    """
    if cur < 10 or prev < 10:
        return ""
    pct = round((cur - prev) * 100 / prev)
    if pct == 0:
        return ""
    return f" {'↑' if pct > 0 else '↓'}{abs(pct)}%"


def render_stats(stats: DailyStats, day: date, last: date | None = None,
                 prev: DailyStats | None = None) -> str:
    """Рендер владельца (П-6): ценность сверху, техника одной строкой внизу.

    prev — окно того же размера непосредственно перед периодом: даёт тренды
    на записях и отменах (В). Пустые секции v2 не показываем — «0 врачей»
    не информация.
    """
    if last is None or last == day:
        header = f"📊 <b>Сводка за {day:%d.%m}</b>"
    else:
        days = (last - day).days + 1
        header = f"📊 <b>Сводка за {days} дн. ({day:%d.%m}–{last:%d.%m})</b>"
    after = (f" (из них {stats.after_hours_booked} — вне рабочих часов)"
             if stats.after_hours_booked else "")
    p95_part = (f" · p95 ответа: {stats.p95_response_sec} с (SLA < 5 с)"
                if stats.p95_response_sec is not None else "")
    booked_trend = _trend(stats.booked, prev.booked) if prev else ""
    cancelled_trend = _trend(stats.cancelled, prev.cancelled) if prev else ""

    sections: list[str] = []  # блоки v2 между «Ценностью» и «Служебным»
    if stats.new_patients or stats.returning_patients:
        sections.append(f"👥 Клиенты\n• новых: {stats.new_patients} · "
                        f"вернувшихся: {stats.returning_patients}")
    if stats.top_doctors:
        # имена расшифрованы из БД — экранируем, сводка уходит с HTML (П-7)
        lines = "\n".join(
            f"• {html.escape(name, quote=False)} — {cnt} зап. "
            f"(≈ {_money(revenue)} сум)"
            for name, cnt, revenue in stats.top_doctors)
        sections.append(f"👨‍⚕️ Топ врачей\n{lines}")
    if stats.hit_service:
        key, cnt = stats.hit_service
        sections.append(f"✨ Хит-услуга\n• {service_label(key, 'ru')} — "
                        f"{cnt} зап.")
    middle = "".join(f"{section}\n" for section in sections)

    return (f"{header}\n"
            f"💰 Ценность\n"
            f"• записей подтверждено: {stats.booked}{booked_trend}{after}\n"
            f"• предотвращено неявок: {stats.prevented_noshows} "
            f"(слотов на ≈ {_money(stats.saved_revenue)} сум освобождено заранее)\n"
            f"• отмен: {stats.cancelled}{cancelled_trend}\n"
            f"• эскалаций к администратору: {stats.escalated}\n"
            f"{middle}"
            f"⚙️ Служебное\n"
            f"• напоминаний: {stats.reminders_sent} · LLM: {stats.llm_requests} "
            f"запросов, {stats.llm_tokens} токенов, "
            f"сбоев: {stats.nlu_failures}, repair: {stats.nlu_repairs}"
            + p95_part)


def render_digest_short(stats: DailyStats) -> str:
    """Короткий вечерний дайджест (В): три строки ценности, без ⚙️-техники.

    Полная сводка дня — за кнопкой «📊 Подробнее» (stats:full), владелец
    раскрывает детали сам, когда интересно.
    """
    after = (f" (из них {stats.after_hours_booked} — вне рабочих часов)"
             if stats.after_hours_booked else "")
    return (f"📊 <b>Итог дня</b>\n"
            f"• записей: {stats.booked}{after}\n"
            f"• предотвращено неявок: {stats.prevented_noshows} "
            f"(≈ {_money(stats.saved_revenue)} сум)\n"
            f"• эскалаций: {stats.escalated}")


QUESTIONS_IN_DIGEST = 10  # cap: дайджест — сводка, не лог


def render_questions(questions: list[str]) -> str:
    """Блок «вопросы без ответа» для дайджеста (П-2б): владелец видит
    спрос, не дёргаясь днём. Тексты уже анонимны (телефоны замаскированы);
    экранируем — дайджест уходит с parse_mode=HTML, пациентский «<» не
    должен ломать парсер (П-7)."""
    shown = questions[:QUESTIONS_IN_DIGEST]
    lines = "\n".join(f"• {html.escape(q, quote=False)}" for q in shown)
    tail = len(questions) - len(shown)
    suffix = f"\n… и ещё {tail}" if tail > 0 else ""
    return f"❓ <b>Вопросы без ответа ({len(questions)})</b>\n{lines}{suffix}"


def should_send_digest(now_local: datetime, last_digest: date | None,
                       hour: int = DIGEST_HOUR) -> bool:
    if now_local.hour < hour:
        return False
    return last_digest is None or last_digest < now_local.date()
