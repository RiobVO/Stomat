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
from navbat.scheduling.calendar_rules import outside_open_hours
from navbat.telegram.admin_texts import at

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
    waitlist_waiting: int = 0  # «сейчас в очереди ожидания» (снимок, не за период)
    # recall (инкремент 4): приглашения на повторный визит и возвраты по ним
    recalls_sent: int = 0
    recalls_returned: int = 0
    recalls_saved: int = 0


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

    # p95 ответа за период: от приёма апдейта до отправленного ответа.
    # Только пациентские чаты: /stats за 30 дней и кнопочная консоль идут той
    # же очередью и тем же воркером, но считают тяжёлые сводки — их секунды
    # не имеют отношения к SLA, который бот обещает пациенту
    p95 = session.execute(
        text("""
            SELECT extract(epoch FROM percentile_cont(0.95)
                   WITHIN GROUP (ORDER BY completed_at - created_at))
            FROM message_queue
            WHERE status = 'done' AND completed_at IS NOT NULL
              AND (completed_at AT TIME ZONE :tz)::date BETWEEN :first AND :last
              AND NOT EXISTS (
                  SELECT 1 FROM clinic c
                  WHERE c.id = current_setting('app.clinic_id')::uuid
                    AND message_queue.tg_chat_id = ANY(c.tg_admin_chat_ids))
        """),
        span,
    ).scalar_one()

    # В: новые/вернувшиеся клиенты. Личность — patient_id, для записей без
    # пациента (демо, ручной онбординг) — tg_chat_id; у patient нет created_at,
    # «первый визит» считаем по appointment.created_at (без новой миграции).
    # Голый hold/expired — не визит; cancelled — визит (человек обращался).
    # 'done' в enum appt_status с 0001 — на будущую ручную отметку визита,
    # engine его пока не ставит.
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

    # Recall: приглашение ушло — «приглашений», пациент записался по нему —
    # «вернулось». Деньги считаем по цене услуги ИСХОДНОГО приёма: приглашение
    # зовёт на неё же, а новая запись к журналу не привязана и джойнить её
    # было бы догадкой. NULL-цены в сумму не входят — неизвестную выручку не
    # выдумываем (то же правило, что у prevented)
    recall = session.execute(
        text("""
            SELECT
                count(*) FILTER (WHERE (r.sent_at AT TIME ZONE :tz)::date
                                       BETWEEN :first AND :last) AS sent,
                count(*) FILTER (WHERE (r.booked_at AT TIME ZONE :tz)::date
                                       BETWEEN :first AND :last) AS returned,
                COALESCE(sum(s.price) FILTER (
                    WHERE (r.booked_at AT TIME ZONE :tz)::date
                          BETWEEN :first AND :last), 0) AS saved
            FROM recall_outreach r
            LEFT JOIN appointment a ON a.id = r.appointment_id
            LEFT JOIN service s ON s.id = a.service_id
        """),
        span,
    ).one()

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
        waitlist_waiting=session.execute(text(
            "SELECT count(*) FROM waitlist WHERE status IN "
            "('waiting', 'notified')")).scalar_one(),
        recalls_sent=recall.sent,
        recalls_returned=recall.returned,
        recalls_saved=int(recall.saved),
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
        text("SELECT working_intervals FROM doctor WHERE is_active")).scalars().all()
    holidays = set(session.execute(
        text("SELECT date FROM holiday WHERE date BETWEEN :first AND :last"),
        {"first": first, "last": last},
    ).scalars())
    return sum(outside_open_hours(moment, schedules, holidays, tz)
               for moment in moments)


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
                 prev: DailyStats | None = None, lang: str = "ru") -> str:
    """Рендер владельца (П-6): ценность сверху, техника одной строкой внизу.

    prev — окно того же размера непосредственно перед периодом: даёт тренды
    на записях и отменах (В). Пустые секции v2 не показываем — «0 врачей»
    не информация. lang — язык админ-чата (карта, №16).
    """
    if last is None or last == day:
        header = at("stats_header_day", lang, date=f"{day:%d.%m}")
    else:
        header = at("stats_header_range", lang, days=(last - day).days + 1,
                    first=f"{day:%d.%m}", last=f"{last:%d.%m}")
    after = (at("stats_after_hours", lang, count=stats.after_hours_booked)
             if stats.after_hours_booked else "")
    # «&lt;» внутри шаблона — сводка уходит с parse_mode=HTML, голый «<»
    # ломает парсер Telegram
    p95_part = (at("stats_p95", lang, seconds=stats.p95_response_sec)
                if stats.p95_response_sec is not None else "")
    booked_trend = _trend(stats.booked, prev.booked) if prev else ""
    cancelled_trend = _trend(stats.cancelled, prev.cancelled) if prev else ""

    sections: list[str] = []  # блоки v2 между «Ценностью» и «Служебным»
    if stats.new_patients or stats.returning_patients:
        sections.append(at("stats_clients", lang, new=stats.new_patients,
                           returning=stats.returning_patients))
    if stats.top_doctors:
        # имена расшифрованы из БД — at() их экранирует, сводка уходит с HTML
        lines = "\n".join(
            at("stats_doctor_line", lang, name=name, count=cnt,
               money=_money(revenue))
            for name, cnt, revenue in stats.top_doctors)
        sections.append(at("stats_top_doctors", lang) + "\n" + lines)
    if stats.hit_service:
        key, cnt = stats.hit_service
        sections.append(at("stats_hit_service", lang,
                           service=service_label(key, lang), count=cnt))
    if stats.waitlist_waiting:
        sections.append(at("stats_waitlist", lang,
                           count=stats.waitlist_waiting))
    if stats.recalls_sent:
        # рассылка не работала — строки нет: витрина владельца без нулей
        sections.append(at("stats_recall", lang, sent=stats.recalls_sent,
                           returned=stats.recalls_returned,
                           money=_money(stats.recalls_saved)))
    middle = "".join(f"{section}\n" for section in sections)

    return (f"{header}\n"
            + at("stats_value_title", lang) + "\n"
            + at("stats_booked", lang, count=stats.booked,
                 trend=booked_trend, after=after) + "\n"
            + at("stats_prevented", lang, count=stats.prevented_noshows,
                 money=_money(stats.saved_revenue)) + "\n"
            + at("stats_cancelled", lang, count=stats.cancelled,
                 trend=cancelled_trend) + "\n"
            + at("stats_escalated", lang, count=stats.escalated) + "\n"
            + middle
            + at("stats_tech", lang, reminders=stats.reminders_sent,
                 requests=stats.llm_requests, tokens=stats.llm_tokens,
                 failures=stats.nlu_failures, repairs=stats.nlu_repairs)
            + p95_part)


def render_digest_short(stats: DailyStats, lang: str = "ru") -> str:
    """Короткий вечерний дайджест (В): три строки ценности, без ⚙️-техники.

    Полная сводка дня — за кнопкой «📊 Подробнее» (stats:full), владелец
    раскрывает детали сам, когда интересно.
    """
    after = (at("stats_after_hours", lang, count=stats.after_hours_booked)
             if stats.after_hours_booked else "")
    queue = (at("digest_waitlist", lang, count=stats.waitlist_waiting)
             if stats.waitlist_waiting else "")
    return (at("digest_title", lang) + "\n"
            + at("digest_booked", lang, count=stats.booked, after=after) + "\n"
            + at("digest_prevented", lang, count=stats.prevented_noshows,
                 money=_money(stats.saved_revenue)) + "\n"
            + at("digest_escalated", lang, count=stats.escalated) + queue)


QUESTIONS_IN_DIGEST = 10  # cap: дайджест — сводка, не лог


def render_questions(questions: list[str], lang: str = "ru") -> str:
    """Блок «вопросы без ответа» для дайджеста (П-2б): владелец видит
    спрос, не дёргаясь днём. Тексты уже анонимны (телефоны замаскированы);
    экранируем — дайджест уходит с parse_mode=HTML, пациентский «<» не
    должен ломать парсер (П-7)."""
    shown = questions[:QUESTIONS_IN_DIGEST]
    lines = "\n".join(f"• {html.escape(q, quote=False)}" for q in shown)
    tail = len(questions) - len(shown)
    suffix = at("questions_more", lang, count=tail) if tail > 0 else ""
    return (at("questions_title", lang, count=len(questions))
            + f"\n{lines}{suffix}")


def should_send_digest(now_local: datetime, last_digest: date | None,
                       hour: int = DIGEST_HOUR) -> bool:
    if now_local.hour < hour:
        return False
    return last_digest is None or last_digest < now_local.date()
