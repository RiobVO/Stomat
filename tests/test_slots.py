"""Генерация свободных слотов против БД: праздники, выходные, занятость."""
from sqlalchemy import text

from conftest import at_tashkent, next_monday, next_sunday


def test_free_day_has_full_grid(sched, doctor_a, service_cleaning):
    day = next_monday()
    slots = sched.find_free_slots(doctor_a, service_cleaning, day)
    assert len(slots) == 16
    assert slots[0].start == at_tashkent(day, "09:00")


def test_holiday_offers_no_slots(sched, admin_engine, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO holiday (clinic_id, date, reason) VALUES (:c, :d, 'Navruz')"),
            {"c": clinic_a, "d": day},
        )
    assert sched.find_free_slots(doctor_a, service_cleaning, day) == []


def test_day_off_offers_no_slots(sched, doctor_a, service_cleaning):
    assert sched.find_free_slots(doctor_a, service_cleaning, next_sunday()) == []


def test_booked_slot_disappears_with_buffer(sched, doctor_a, service_cleaning):
    day = next_monday()
    appt_id = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"))
    sched.confirm(appt_id)

    starts = {s.start for s in sched.find_free_slots(doctor_a, service_cleaning, day)}
    assert at_tashkent(day, "09:00") not in starts
    # буфер 10 мин: запись «живёт» до 09:40 → слот 09:30 тоже недоступен
    assert at_tashkent(day, "09:30") not in starts
    assert at_tashkent(day, "10:00") in starts
