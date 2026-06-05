"""Заполнение таблицы holiday через onboard: госпраздники РУз + хайиты (Ф1.5).

Хайиты плавающие — даты объявляются ежегодно и задаются параметром --hayit.
Год тестов — следующий календарный: даты гарантированно в будущем.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from navbat.onboard import seed_holidays
from navbat.scheduling.engine import SchedulingEngine

NEXT_YEAR = date.today().year + 1


def holiday_rows(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT date, reason FROM holiday WHERE clinic_id = :c "
                 "ORDER BY date"),
            {"c": clinic_id},
        ).all()


def test_seed_inserts_fixed_holidays_and_hayits(app_session_factory, admin_engine,
                                                clinic_a):
    added, skipped = seed_holidays(app_session_factory, clinic_a, NEXT_YEAR,
                                   ["20.03", "27.05"])
    assert (added, skipped) == (9, 0), "7 госпраздников + 2 хайита"

    rows = holiday_rows(admin_engine, clinic_a)
    reasons = {row.date: row.reason for row in rows}
    assert "Навруз" in reasons[date(NEXT_YEAR, 3, 21)]
    assert date(NEXT_YEAR, 9, 1) in reasons          # День независимости
    assert reasons[date(NEXT_YEAR, 3, 20)] == "Хайит"
    assert reasons[date(NEXT_YEAR, 5, 27)] == "Хайит"


def test_seed_is_idempotent(app_session_factory, admin_engine, clinic_a):
    seed_holidays(app_session_factory, clinic_a, NEXT_YEAR, ["20.03"])
    added, skipped = seed_holidays(app_session_factory, clinic_a, NEXT_YEAR,
                                   ["20.03", "27.05"])
    assert (added, skipped) == (1, 8), "досеялся только новый хайит"
    assert len(holiday_rows(admin_engine, clinic_a)) == 9


def test_bad_hayit_date_rejected(app_session_factory, clinic_a):
    with pytest.raises(ValueError):
        seed_holidays(app_session_factory, clinic_a, NEXT_YEAR, ["31.02"])
    with pytest.raises(ValueError):
        seed_holidays(app_session_factory, clinic_a, NEXT_YEAR, ["abc"])


def test_seeded_holiday_closes_slots(app_session_factory, admin_engine, clinic_a,
                                     doctor_a, service_cleaning):
    seed_holidays(app_session_factory, clinic_a, NEXT_YEAR, [])
    # берём праздник на рабочий день графика (пн–сб): без сида слоты были бы
    workday_holiday = next(
        row.date for row in holiday_rows(admin_engine, clinic_a)
        if row.date.weekday() < 6
    )
    sched = SchedulingEngine(app_session_factory, clinic_a)
    assert sched.find_free_slots(doctor_a, service_cleaning, workday_holiday) == []
