"""Демо-история для витрины владельца: наполняет прошлое работой бота.

    python -m navbat.onboard --demo-history [--days 14]
    python -m navbat.onboard --demo-history-clear      # убрать следы

Зачем: на чистой базе `/stats 7` показывает нули по всем строкам, а секции
«Клиенты», «Топ врачей» и «Хит-услуга» не рендерятся вовсе — главный
денежный аргумент показа выглядит пустым экраном (docs/SALES_READINESS.md,
№4). Сидер создаёт правдоподобную неделю-другую: записи (часть оформлена
вне рабочих часов), отмены из напоминания с суммой освобождённых слотов,
новые и вернувшиеся пациенты.

Границы (ревью сидера):
- только демо-клиника — гейт в CLI: синтетика в базе живой клиники портит
  её отчётность, а отката «на глаз» там не будет;
- только прошлое и только внутри смен врача: будущие записи заняли бы слоты
  живого сценария, а приёмы в выходной или обед вызвали бы вопрос владельца;
- очередь ожидания НЕ сеется: активные строки заставляют матчер каждые
  30 секунд слать пуши в несуществующие чаты, а при ошибке доставки гасят
  их — метрика «в очереди» пропадала бы посреди показа. Очередь показывается
  вживую, когда пациент жмёт 🔔 на шаге «слотов нет»;
- идемпотентно, есть обратная команда.

Данные синтетические, PII в них нет: пациенты обезличены (tg_chat_id из
служебного диапазона, без имён и телефонов).
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
RECENT_BONUS = 4
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
# шаг сетки слотов живой записи (scheduling.calendar_rules.slot_candidates)
SLOT_STEP_MIN = 30
# за сколько часов до приёма оформлена сегодняшняя заявка
SAME_DAY_LEAD_HOURS = 3
# раньше этого часа заявок не бывает даже ночью
EARLIEST_BOOKING_HOUR = 6


def _hour_for(index: int, after_hours: bool) -> int:
    """Час оформления записи: ночные — когда администратор спит."""
    if after_hours:
        return (21, 22, 23, 6, 7)[index % 5]
    return (9, 10, 11, 12, 14, 15, 16)[index % 7]


def seed_demo_history(session_factory, clinic_id: uuid.UUID,
                      days: int = 14, now: datetime | None = None) -> int:
    """Наполнить прошлые `days` дней. Возвращает число созданных записей.

    Идемпотентность скользящая: день, в котором история уже есть, не трогаем,
    а пустые дни окна досеиваем — между показами «сегодня» уезжает вперёд.

    `now` инжектируется тестами (конвенция проекта — время тестируемо):
    сколько истории попадёт в сегодняшний день, зависит от часа прогона."""
    with tenant_transaction(session_factory, clinic_id) as session:
        tz = _clinic_zone(session)
        if tz is None:
            # сеять историю до `--demo` — типовой промах порядка команд; ответ
            # даёт CLI, здесь важно не отдать сырой NoResultFound
            log.warning("демо-история: демо-клиники нет в базе")
            return 0
        doctors = session.execute(text(
            "SELECT id, buffer_min, working_intervals FROM doctor "
            "WHERE is_active ORDER BY id")).all()
        services = session.execute(text(
            "SELECT id, name, duration_min FROM service WHERE is_active "
            "ORDER BY name")).all()
        if not doctors or not services:
            log.warning("демо-история: нет активных врачей или услуг")
            return 0
        now = now.astimezone(tz) if now is not None else datetime.now(tz)
        today = now.date()
        # дни, закрытые владельцем: живая запись туда не пустила бы
        # (scheduling.engine смотрит holiday), значит и сидер не должен
        closed = set(session.execute(
            text("SELECT date FROM holiday WHERE date >= :first"),
            {"first": today - timedelta(days=days)}).scalars())
        # дни, наполненные прошлым запуском: окно скользит вместе с «сегодня»,
        # поэтому сид досеивает пустые дни, а наполненные не трогает. Прежний
        # гейт «история уже есть» смотрел на факт наличия строк — и второй
        # показ шёл с пустой свежей неделей при «истории» в базе
        seeded = set(session.execute(
            text("SELECT DISTINCT (lower(time_range) AT TIME ZONE :tz)::date "
                 "FROM appointment WHERE source = :src"),
            {"tz": tz.key, "src": DEMO_SOURCE}).scalars())
        created = 0
        # включая сегодня (offset=0): владелец жмёт «📊 Статистика», а консоль
        # открывает сводку ЗА ДЕНЬ — пустой сегодняшний день снова показывал
        # покупателю нули (живой тык 28.07)
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            if day in closed or day in seeded:
                continue
            per_day, nightly = DAILY_PLAN[offset % len(DAILY_PLAN)]
            if offset <= days // 2:
                per_day += RECENT_BONUS
            # смены врачей в этот день: в выходной и в обед приёмов не бывает,
            # иначе владелец увидит их в статистике и справедливо спросит,
            # откуда они взялись (ревью сидера)
            shifts = {doc.id: _day_shifts(doc, day, tz) for doc in doctors}
            if not any(shifts.values()):
                continue
            # курсор приёма по каждому врачу: записи не должны перекрываться
            # (в БД стоит exclusion constraint с буфером — сидер обязан жить
            # по тем же правилам, что и живая запись)
            cursors = {doc.id: (shifts[doc.id][0][0] if shifts[doc.id] else None)
                       for doc in doctors}
            for index in range(per_day):
                created += _make_appointment(
                    session, tz, day, index, created, doctors, services,
                    cursors, shifts, after_hours=index < nightly,
                    # сегодняшние приёмы — только те, что уже прошли: будущие
                    # заняли бы слоты, которые показываются вживую
                    not_after=now if offset == 0 else None)
    log.info("демо-история: создано записей — %d", created)
    return created


def _clinic_zone(session: Session) -> ZoneInfo | None:
    """Таймзона клиники; None — клиники нет (сеяли до `--demo`)."""
    zone = session.execute(text(
        "SELECT timezone FROM clinic "
        "WHERE id = current_setting('app.clinic_id')::uuid")).scalar_one_or_none()
    return ZoneInfo(zone) if zone else None


def _pick_service(services, created: int):
    """Услуга по весам спроса; если клиника ведёт не весь каталог —
    круг по тому, что есть."""
    by_name = {row.name: row for row in services}
    wanted = [name for name in SERVICE_WEIGHTS if name in by_name]
    if not wanted:
        return services[created % len(services)]
    return by_name[wanted[created % len(wanted)]]


def _doctor_queue(doctors, created: int) -> list:
    """Очередь кандидатов на запись: сначала «дежурный», затем остальные.

    Нагрузка неровная — ровно поделённая пополам выглядит сгенерированной,
    поэтому дежурный идёт по паттерну с весом у первого врача. Но в паттерне
    обязан быть КАЖДЫЙ активный врач, а очередь — содержать всех: прежние
    веса (0, 1, 0, 1, 0, 0, 1) при трёх врачах не давали третьему ни одной
    записи (он исчезал из «Топ врачей»), а день, в который свободен только
    он, обрывался на первой же записи."""
    order = tuple(range(len(doctors)))
    pattern = order + order[:1] + order[::-1]  # первый чаще остальных
    first = pattern[created % len(pattern)]
    return [doctors[(first + step) % len(doctors)] for step in range(len(doctors))]


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _day_shifts(doctor, day: date, tz: ZoneInfo) -> list[tuple]:
    """Смены врача в этот день из его working_intervals — [(начало, конец)].

    Пустой список = выходной. Обед живёт разрывом между сменами, отдельной
    константы не нужно: график ведёт клиника, а не сидер."""
    intervals = (doctor.working_intervals or {}).get(WEEKDAYS[day.weekday()], [])
    spans = []
    midnight = datetime.combine(day, datetime.min.time(), tz)
    for span in intervals:
        lo_h, lo_m = (int(part) for part in str(span[0]).split(":"))
        hi_h, hi_m = (int(part) for part in str(span[1]).split(":"))
        spans.append((midnight.replace(hour=lo_h, minute=lo_m),
                      midnight.replace(hour=hi_h, minute=hi_m)))
    return spans


def _fit_into_shift(start: datetime, minutes: int, shifts) -> datetime | None:
    """Ближайшее начало приёма на сетке слотов, целиком помещающегося
    в смену; None — рабочее время дня исчерпано.

    Сетка обязательна: живая запись предлагает слоты через SLOT_STEP от
    начала смены, и приём в 09:40 виден владельцу как чужеродный."""
    for lo, hi in shifts:
        candidate = max(start, lo)
        steps = -(-int((candidate - lo).total_seconds() // 60) // SLOT_STEP_MIN)
        candidate = lo + timedelta(minutes=steps * SLOT_STEP_MIN)
        if candidate + timedelta(minutes=minutes) <= hi:
            return candidate
    return None


def _make_appointment(session: Session, tz: ZoneInfo, day: date, index: int,
                      created: int, doctors, services, cursors, shifts,
                      after_hours: bool, not_after: datetime | None = None) -> int:
    """Одна запись: приём внутри смены врача, оформление — раньше приёма.

    Цикл — про занятые слоты: сидер не знает, что стоит в прошлом врача, и
    узнаёт это от БД. Он конечен, потому что каждая неудача продвигает курсор
    врача на шаг сетки, а исчерпанная смена даёт `start is None`."""
    service = _pick_service(services, created)
    while True:
        # если у дежурного кончилась смена, все следующие итерации дня выбирали
        # бы его же и день обрывался на середине — идём по очереди дальше
        doctor = start = None
        for candidate in _doctor_queue(doctors, created):
            if (not shifts.get(candidate.id)
                    or cursors.get(candidate.id) is None):
                continue  # у этого врача сегодня выходной
            at_time = _fit_into_shift(cursors[candidate.id], service.duration_min,
                                      shifts[candidate.id])
            if at_time is not None:
                doctor, start = candidate, at_time
                break
        if start is None:
            return 0  # смены всех врачей на сегодня исчерпаны
        doctor_id = doctor.id
        finish = start + timedelta(minutes=service.duration_min)
        if not_after is not None and finish > not_after:
            return 0  # сегодняшний день наполняем только прошедшими часами
        if not_after is None:
            # оформление — накануне: ночные заявки и есть «пока клиника спала»
            booked_at = datetime.combine(
                day - timedelta(days=1), datetime.min.time(),
                tz).replace(hour=_hour_for(index, after_hours))
        else:
            # сегодняшний день оформляется сегодня же: сводка за день считает
            # confirm-аудиты и created_at (stats.py), а не время приёма — с
            # оформлением «вчера» первый экран покупателя оставался пустым
            # даже при прошедших приёмах (повторное ревью сидера, блокер)
            earliest = datetime.combine(day, datetime.min.time(), tz).replace(
                hour=EARLIEST_BOOKING_HOUR)
            booked_at = max(start - timedelta(hours=SAME_DAY_LEAD_HOURS), earliest)
        # часть визитов — повторные: остаток по счётчику переиспользует чат
        # более раннего пациента. Точная доля неважна, витрине нужны обе группы;
        # ровное «каждый третий» ставило бы повтор рядом с оригиналом, а stats
        # считает вернувшимся того, чей ПЕРВЫЙ визит раньше периода — в окне
        # семи дней такие возвраты схлопнулись бы в ноль
        chat = CHAT_BASE - (created // RETURNING_EVERY if created % RETURNING_EVERY
                            else created)
        cancelled = created % CANCEL_EVERY == CANCEL_EVERY - 1
        appointment_id = uuid.uuid4()
        if not _try_insert(session, {
                "id": appointment_id, "doc": doctor_id, "svc": service.id,
                "lo": start, "hi": finish, "buf": doctor.buffer_min,
                "src": DEMO_SOURCE, "chat": chat,
                "status": "cancelled" if cancelled else "booked",
                "made": booked_at}):
            # слот занят тем, кого сидер не видит: ручное событие из календаря
            # врача или запись, оставшаяся после показа. Двигаемся по сетке
            cursors[doctor_id] = start + timedelta(minutes=SLOT_STEP_MIN)
            continue
        cursors[doctor_id] = finish + timedelta(minutes=doctor.buffer_min)
        _audit(session, appointment_id, "confirm", "bot", booked_at)
        if cancelled:
            # actor='reminder' — топливо метрики «предотвращено неявок»: пациент
            # предупредил заранее по напоминанию, слот вернулся в продажу
            _audit(session, appointment_id, "cancel", "reminder",
                   start - timedelta(hours=3))
        return 1


def _try_insert(session: Session, params: dict) -> bool:
    """Вставить запись; False — слот занят, и это не повод падать.

    Занятость в проекте гарантирует БД (exclusion constraint), а не код —
    сидер живёт по тому же правилу, что живая запись в scheduling.engine.
    SAVEPOINT здесь обязателен: вся история наливается ОДНОЙ транзакцией, и
    без него первое пересечение с ручным событием врача (его личный календарь
    импортируется вместе с прошлым) откатывало бы весь сид, оставляя витрину
    пустой прямо перед показом."""
    savepoint = session.begin_nested()
    try:
        session.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, service_id, "
                 "time_range, buffer_min, status, source, tg_chat_id, created_at) "
                 "VALUES (:id, current_setting('app.clinic_id')::uuid, :doc, :svc, "
                 "tstzrange(:lo, :hi, '[)'), :buf, :status, :src, :chat, :made)"),
            params)
    except IntegrityError as error:
        savepoint.rollback()
        if getattr(error.orig, "sqlstate", None) != "23P01":  # exclusion_violation
            raise  # не пересечение (FK, дубль) — прятать нельзя
        return False
    savepoint.commit()
    return True


def _audit(session: Session, appointment_id: uuid.UUID, action: str,
           actor: str, at: datetime) -> None:
    session.execute(
        text("INSERT INTO appointment_audit (clinic_id, appointment_id, actor, "
             "action, at) VALUES (current_setting('app.clinic_id')::uuid, "
             ":appt, :actor, :action, :at)"),
        {"appt": appointment_id, "actor": actor, "action": action, "at": at})


def count_demo_history(session_factory, clinic_id: uuid.UUID, days: int = 14,
                       now: datetime | None = None) -> int:
    """Сколько демо-истории ВИДИТ СВОДКА в окне последних `days` дней.

    Нужно CLI, чтобы отличить «окно уже наполнено» от «наливать некуда»: ноль
    созданных записей сам по себе не говорит, что витрина не пуста (ре-ревью).

    Считаем то же, что считает витрина, — confirm-аудиты (stats.py), а не сами
    приёмы: оформление прошлых приёмов сид датирует НАКАНУНЕ, поэтому приёмы
    самого старого дня окна сводке не видны. Счёт по приёмам отвечал «уже
    есть» над сводкой из нулей — ровно та ложь, от которой заведён [FAIL].

    Окно мерим локальными ДАТАМИ, как ведёт его сид. Абсолютное «now() минус N
    суток» отрезало бы утро самого старого дня: при `days=0` не видно ни одной
    прошедшей записи вовсе, а после наполнения в субботу повтор в воскресенье
    вечером терял субботнее утро."""
    with tenant_transaction(session_factory, clinic_id) as session:
        tz = _clinic_zone(session)
        if tz is None:
            return 0
        today = (now.astimezone(tz) if now is not None else datetime.now(tz)).date()
        return session.execute(
            text("SELECT count(*) FROM appointment_audit aa "
                 "JOIN appointment a ON a.id = aa.appointment_id "
                 "WHERE a.source = :src AND aa.action = 'confirm' "
                 "AND (aa.at AT TIME ZONE :tz)::date >= :first"),
            {"src": DEMO_SOURCE, "tz": tz.key,
             "first": today - timedelta(days=days)}).scalar_one()


def clear_demo_history(session_factory, clinic_id: uuid.UUID) -> int:
    """Убрать демо-историю целиком: записи и их аудит.

    Откат обязателен — без него следы синтетики остаются в базе навсегда
    (ревью сидера). Порядок важен: аудит ссылается на записи."""
    with tenant_transaction(session_factory, clinic_id) as session:
        session.execute(
            text("DELETE FROM appointment_audit WHERE appointment_id IN "
                 "(SELECT id FROM appointment WHERE source = :src)"),
            {"src": DEMO_SOURCE})
        removed = session.execute(
            text("DELETE FROM appointment WHERE source = :src"),
            {"src": DEMO_SOURCE}).rowcount
    log.info("демо-история удалена: записей — %d", removed)
    return removed
