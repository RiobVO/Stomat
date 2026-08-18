"""Гонка: 50 одновременных запросов на один слот → ровно 1 запись (приёмочный тест брифа)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.pool import NullPool

from navbat.db.base import make_app_engine, make_session_factory
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import SlotTakenError

from conftest import at_tashkent, next_monday

N_CLIENTS = 50

# дублирует ключ engine._lock_doctor — advisory-лок на конкретного врача
_DOCTOR_LOCK = ("SELECT pg_advisory_xact_lock("
                "hashtextextended(CAST(:d AS text), 0))")


def test_fifty_concurrent_requests_one_booking(
    admin_engine, clinic_a, doctor_a, service_cleaning
):
    day = next_monday()
    start = at_tashkent(day, "09:00")

    # NullPool: каждый поток получает собственное соединение — честная конкуренция
    engine = make_app_engine(poolclass=NullPool)
    sched = SchedulingEngine(make_session_factory(engine), clinic_a)

    def try_book(_):
        try:
            appt_id = sched.hold(doctor_a, service_cleaning, start)
            sched.confirm(appt_id)
            return "booked"
        except SlotTakenError:
            return "taken"

    try:
        with ThreadPoolExecutor(max_workers=N_CLIENTS) as pool:
            results = list(pool.map(try_book, range(N_CLIENTS)))
    finally:
        engine.dispose()

    assert results.count("booked") == 1
    assert results.count("taken") == N_CLIENTS - 1

    with admin_engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM appointment WHERE doctor_id = :d "
                 "AND status IN ('hold', 'booked')"),
            {"d": doctor_a},
        ).scalar_one()
    assert active == 1


def _is_deadlock(err) -> bool:
    return err is not None and "deadlock" in str(err).lower()


def test_reschedule_does_not_deadlock_with_concurrent_doctor_write(
    app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning
):
    # M1: reschedule брал row-lock (FOR UPDATE) ДО advisory-лока — инверсия
    # порядка против hold/confirm (advisory → row). Детерминированный репро:
    # сессия B держит advisory на врача и пишет ту же строку → если reschedule
    # успел взять row-lock и ждёт advisory, образуется цикл (40P01).
    day = next_monday()
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appt = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"))
    sched.confirm(appt)

    err: dict = {}

    def reschedule_thread():
        try:
            sched.reschedule(appt, at_tashkent(day, "10:00"))
        except Exception as e:  # noqa: BLE001 — фиксируем для проверки на дедлок
            err["resched"] = e

    # B (admin, обходит RLS) захватывает advisory врача и держит транзакцию
    conn_b = admin_engine.connect()
    trans_b = conn_b.begin()
    conn_b.execute(text(_DOCTOR_LOCK), {"d": doctor_a})

    b_err = None
    try:
        worker = threading.Thread(target=reschedule_thread)
        worker.start()
        # даём reschedule дойти до ожидания advisory (pre-fix он уже держит row-lock)
        time.sleep(1.0)
        # B пишет ту же строку → pre-fix замыкает цикл с row-lock'ом reschedule
        try:
            conn_b.execute(
                text("UPDATE appointment SET buffer_min = buffer_min WHERE id = :id"),
                {"id": appt},
            )
            trans_b.commit()
        except Exception as e:  # noqa: BLE001
            b_err = e
            trans_b.rollback()
        worker.join(timeout=15)
        assert not worker.is_alive(), "reschedule завис — вероятен неразрешённый дедлок"
    finally:
        conn_b.close()

    assert not _is_deadlock(err.get("resched")), f"reschedule в дедлоке: {err.get('resched')}"
    assert not _is_deadlock(b_err), f"конкурентная запись в дедлоке: {b_err}"


def test_calendar_import_takes_doctor_lock_first(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a,
                                                 service_cleaning):
    """Синк календаря пишет занятость врача — значит идёт под тем же
    advisory-локом и берёт его ПЕРВЫМ.

    Иначе порядок захвата инвертируется: импорт держит незакоммиченные
    строки и ждёт внутри exclusion-проверки чужой INSERT, а пациентский
    hold ждёт его же — Postgres рвёт одну из транзакций (40P01), и падает
    либо календарный цикл, либо запись пациента.
    """
    from conftest import at_tashkent, next_monday
    from test_gcal_export import bind_calendar, make_sync

    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sync, api = make_sync(app_session_factory, clinic_a)
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))

    done = threading.Event()
    failure: dict = {}

    def import_thread():
        try:
            sync.sync_doctor(doctor_a)
        except Exception as e:  # noqa: BLE001 — фиксируем для отчёта
            failure["sync"] = e
        finally:
            done.set()

    # сторонняя сессия держит advisory-лок врача
    conn = admin_engine.connect()
    trans = conn.begin()
    conn.execute(text(_DOCTOR_LOCK), {"d": doctor_a})
    try:
        worker = threading.Thread(target=import_thread)
        worker.start()
        assert not done.wait(2.0), "импорт пишет занятость мимо advisory-лока врача"
    finally:
        trans.rollback()
        conn.close()

    worker.join(timeout=15)
    assert done.is_set(), "импорт не дождался освобождения лока"
    assert "sync" not in failure, f"импорт упал: {failure.get('sync')}"
    with admin_engine.begin() as check:
        imported = check.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'gcal_import' "
            "AND status = 'booked'")).scalar_one()
    assert imported == 1
