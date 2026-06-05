"""Гонка: 50 одновременных запросов на один слот → ровно 1 запись (приёмочный тест брифа)."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.pool import NullPool

from navbat.db.base import make_app_engine, make_session_factory
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import SlotTakenError

from conftest import at_tashkent, next_monday

N_CLIENTS = 50


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
