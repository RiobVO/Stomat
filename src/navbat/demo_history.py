"""Демо-история для витрины владельца: наполняет прошлое работой бота.

    python -m navbat.onboard --demo-history [--days 14]

Зачем: на чистой базе `/stats 7` показывает нули по всем строкам, а секции
«Клиенты», «Топ врачей» и «Хит-услуга» не рендерятся вовсе — главный
денежный аргумент показа выглядит пустым экраном (docs/SALES_READINESS.md,
№4). Сидер создаёт правдоподобную неделю-другую: записи (часть оформлена
вне рабочих часов), отмены из напоминания с суммой освобождённых слотов,
новые и вернувшиеся пациенты, очередь ожидания.

Только прошлое: будущие записи заняли бы слоты, которые показываются
вживую. Идемпотентно — повторный прогон перед встречей ничего не удваивает.
Данные синтетические, PII в них нет: пациенты обезличены (tg_chat_id из
демо-диапазона, без имён и телефонов).
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.db.base import tenant_transaction

log = logging.getLogger("navbat.demo_history")

# метка источника: по ней сидер узнаёт свои записи и не плодит дубли
DEMO_SOURCE = "demo_history"
# чаты синтетических пациентов — заведомо вне диапазона живых Telegram-id
CHAT_BASE = -900_000_000

# «расписание спроса» недели: сколько записей в день и сколько из них ночных.
# Цифры не круглые — ровный поток читается как выдумка
DAILY_PLAN = ((4, 1), (5, 2), (3, 1), (6, 2), (5, 1), (7, 3), (2, 1))
# свежая половина периода плотнее старой: на показе тренд к прошлой неделе
# обязан смотреть вверх, «↓19%» под словами о росте выручки хуже пустого экрана
RECENT_BONUS = 2
# спрос клиники держат массовые услуги; премиум редок — иначе «Хит-услуга:
# Брекеты» читается как выдумка. Порядок = приоритет, повторы = вес
SERVICE_WEIGHTS = ("cleaning", "checkup", "cleaning", "filling", "checkup",
                   "xray", "cleaning", "filling", "whitening", "checkup",
                   "extraction", "cleaning", "crown", "filling")
# каждая N-я запись отменяется из напоминания — это и есть «предотвращено
# неявок»: слот вернулся в продажу заранее, а не сгорел
CANCEL_EVERY = 6
# доля вернувшихся: каждый третий пациент приходит повторно
RETURNING_EVERY = 3


def _hour_for(index: int, after_hours: bool) -> int:
    """Час оформления записи: ночные — когда администратор спит."""
    if after_hours:
        return (21, 22, 23, 6, 7)[index % 5]
    return (9, 10, 11, 12, 14, 15, 16)[index % 7]


def seed_demo_history(session_factory, clinic_id: uuid.UUID,
                      days: int = 14) -> int:
    """Наполнить прошлые `days` дней. Возвращает число созданных записей."""
    with tenant_transaction(session_factory, clinic_id) as session:
        if _already_seeded(session):
            log.info("демо-история уже есть — пропускаю")
            return 0
        tz = ZoneInfo(session.execute(text(
            "SELECT timezone FROM clinic "
            "WHERE id = current_setting('app.clinic_id')::uuid")).scalar_one())
        doctors = session.execute(text(
            "SELECT id, buffer_min FROM doctor WHERE is_active "
            "ORDER BY id")).all()
        services = session.execute(text(
            "SELECT id, name, duration_min FROM service WHERE is_active "
            "ORDER BY name")).all()
        if not doctors or not services:
            log.warning("демо-история: нет активных врачей или услуг")
            return 0
        now = datetime.now(tz)
        today = now.date()
        created = 0
        # включая сегодня (offset=0): владелец жмёт «📊 Статистика», а консоль
        # открывает сводку ЗА ДЕНЬ — пустой сегодняшний день снова показывал
        # покупателю нули (живой тык 28.07)
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            per_day, nightly = DAILY_PLAN[offset % len(DAILY_PLAN)]
            if offset <= days // 2:
                per_day += RECENT_BONUS
            # курсор приёма по каждому врачу: записи не должны перекрываться
            # (в БД стоит exclusion constraint с буфером — сидер обязан жить
            # по тем же правилам, что и живая запись)
            cursors = {doc.id: _day_start(day, tz) for doc in doctors}
            for index in range(per_day):
                created += _make_appointment(
                    session, tz, day, index, created, doctors, services,
                    cursors, after_hours=index < nightly,
                    # сегодняшние приёмы — только те, что уже прошли: будущие
                    # заняли бы слоты, которые показываются вживую
                    not_after=now if offset == 0 else None)
        _seed_waitlist(session, services)
    log.info("демо-история: создано записей — %d", created)
    return created


def _already_seeded(session: Session) -> bool:
    return bool(session.execute(
        text("SELECT 1 FROM appointment WHERE source = :src LIMIT 1"),
        {"src": DEMO_SOURCE}).scalar_one_or_none())


def _pick_service(services, created: int):
    """Услуга по весам спроса; если клиника ведёт не весь каталог —
    круг по тому, что есть."""
    by_name = {row.name: row for row in services}
    wanted = [name for name in SERVICE_WEIGHTS if name in by_name]
    if not wanted:
        return services[created % len(services)]
    return by_name[wanted[created % len(wanted)]]


def _pick_doctor(doctors, created: int):
    """Нагрузка неровная: ровно поделённая пополам выглядит сгенерированной."""
    pattern = (0, 1, 0, 1, 0, 0, 1) if len(doctors) > 1 else (0,)
    return doctors[pattern[created % len(pattern)] % len(doctors)]


DAY_OPEN, LUNCH_FROM, LUNCH_TO, DAY_CLOSE = 9, 13, 14, 18


def _day_start(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, datetime.min.time(), tz).replace(hour=DAY_OPEN)


def _skip_lunch(start: datetime) -> datetime:
    """Обед клиники 13:00–14:00 — приёмов там не бывает."""
    if LUNCH_FROM <= start.hour < LUNCH_TO:
        return start.replace(hour=LUNCH_TO, minute=0)
    return start


def _make_appointment(session: Session, tz: ZoneInfo, day: date, index: int,
                      created: int, doctors, services, cursors,
                      after_hours: bool, not_after: datetime | None = None) -> int:
    """Одна запись: приём в рабочее окно, оформление — раньше приёма."""
    doctor = _pick_doctor(doctors, created)
    doctor_id = doctor.id
    service = _pick_service(services, created)
    start = _skip_lunch(cursors[doctor_id])
    finish = start + timedelta(minutes=service.duration_min)
    if finish.hour >= DAY_CLOSE:
        return 0  # день врача заполнен — не выдумываем приём после закрытия
    if not_after is not None and finish > not_after:
        return 0  # сегодняшний день наполняем только прошедшими часами
    cursors[doctor_id] = finish + timedelta(minutes=doctor.buffer_min)
    # оформление — накануне: ночные заявки и есть «пока клиника спала»
    booked_at = datetime.combine(day - timedelta(days=1), datetime.min.time(),
                                 tz).replace(hour=_hour_for(index, after_hours))
    if not_after is not None and booked_at > not_after:
        booked_at = not_after - timedelta(hours=1)
    # вернувшийся пациент повторяет чат прошлого визита
    chat = CHAT_BASE - (created // RETURNING_EVERY if created % RETURNING_EVERY
                        else created)
    cancelled = created % CANCEL_EVERY == CANCEL_EVERY - 1
    appointment_id = uuid.uuid4()
    session.execute(
        text("INSERT INTO appointment (id, clinic_id, doctor_id, service_id, "
             "time_range, status, source, tg_chat_id, created_at) VALUES "
             "(:id, current_setting('app.clinic_id')::uuid, :doc, :svc, "
             "tstzrange(:lo, :hi, '[)'), :status, :src, :chat, :made)"),
        {"id": appointment_id, "doc": doctor_id, "svc": service.id,
         "lo": start, "hi": finish, "src": DEMO_SOURCE, "chat": chat,
         "status": "cancelled" if cancelled else "booked", "made": booked_at})
    _audit(session, appointment_id, "confirm", "bot", booked_at)
    if cancelled:
        # actor='reminder' — топливо метрики «предотвращено неявок»: пациент
        # предупредил заранее по напоминанию, слот вернулся в продажу
        _audit(session, appointment_id, "cancel", "reminder",
               start - timedelta(hours=3))
    return 1


def _audit(session: Session, appointment_id: uuid.UUID, action: str,
           actor: str, at: datetime) -> None:
    session.execute(
        text("INSERT INTO appointment_audit (clinic_id, appointment_id, actor, "
             "action, at) VALUES (current_setting('app.clinic_id')::uuid, "
             ":appt, :actor, :action, :at)"),
        {"appt": appointment_id, "actor": actor, "action": action, "at": at})


def _seed_waitlist(session: Session, services) -> None:
    """Пара человек в очереди ожидания: «непокрытый спрос как на ладони»."""
    for shift, service in enumerate(services[:2]):
        session.execute(
            text("INSERT INTO waitlist (clinic_id, service_id, tg_chat_id, lang) "
                 "VALUES (current_setting('app.clinic_id')::uuid, :svc, :chat, 'ru') "
                 "ON CONFLICT DO NOTHING"),
            {"svc": service.id, "chat": CHAT_BASE - 5_000 - shift})
