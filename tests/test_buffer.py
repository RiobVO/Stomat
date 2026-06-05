"""Буфер гарантируется БД, а не кодом: прямой INSERT мимо движка отклоняется."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import at_tashkent, next_monday


def _raw_insert(admin_engine, clinic_id, doctor_id, service_id, start, end):
    """Вставка в обход движка (админом, мимо RLS) — проверяем именно constraint."""
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO appointment "
                "(clinic_id, doctor_id, service_id, time_range, buffer_min, status, source) "
                "VALUES (:c, :d, :s, tstzrange(:f, :t, '[)'), 10, 'booked', 'manual')"
            ),
            {"c": clinic_id, "d": doctor_id, "s": service_id, "f": start, "t": end},
        )


def test_buffer_enforced_by_database(
    sched, admin_engine, clinic_a, doctor_a, service_cleaning
):
    day = next_monday()
    appt_id = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "10:00"))
    sched.confirm(appt_id)  # занято 10:00–10:30, буфер 10 мин → до 10:40

    # вплотную после записи — внутри буфера: БД обязана отклонить
    with pytest.raises(IntegrityError) as excinfo:
        _raw_insert(
            admin_engine, clinic_a, doctor_a, service_cleaning,
            at_tashkent(day, "10:30"), at_tashkent(day, "11:00"),
        )
    assert "appointment_no_overlap" in str(excinfo.value)

    # после буфера — проходит
    _raw_insert(
        admin_engine, clinic_a, doctor_a, service_cleaning,
        at_tashkent(day, "10:40"), at_tashkent(day, "11:10"),
    )
