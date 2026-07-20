"""Резолвинг date_ref/time_ref — чистые функции, без БД.

Конвенции (см. план инкремента 2):
- weekday_* — ближайший такой день, включая сегодня;
- next_week — понедельник следующей недели;
- explicit_DD.MM — текущий год, прошедшая дата -> следующий год;
- окна time_ref: morning < 12:00, afternoon 12-17, evening >= 17, HH:MM — точно.
"""
from __future__ import annotations

from datetime import date, time

import pytest

from navbat.dialog.dates import matches_time_ref, resolve_date_ref

FRI = date(2026, 6, 5)  # пятница


# ── date_ref ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, expected", [
    ("today", date(2026, 6, 5)),
    ("tomorrow", date(2026, 6, 6)),
    ("after_tomorrow", date(2026, 6, 7)),
])
def test_relative_days(ref, expected):
    assert resolve_date_ref(ref, today=FRI) == expected


def test_weekday_same_day_is_today():
    assert resolve_date_ref("weekday_fri", today=FRI) == FRI


def test_weekday_next_occurrence():
    assert resolve_date_ref("weekday_mon", today=FRI) == date(2026, 6, 8)
    assert resolve_date_ref("weekday_thu", today=FRI) == date(2026, 6, 11)


def test_next_week_is_next_monday():
    assert resolve_date_ref("next_week", today=FRI) == date(2026, 6, 8)
    # с понедельника «на следующей неделе» — не сегодня, а через неделю
    assert resolve_date_ref("next_week", today=date(2026, 6, 8)) == date(2026, 6, 15)


def test_explicit_future_current_year():
    assert resolve_date_ref("explicit_15.06", today=FRI) == date(2026, 6, 15)


def test_explicit_today_is_today():
    assert resolve_date_ref("explicit_05.06", today=FRI) == FRI


def test_explicit_passed_rolls_to_next_year():
    assert resolve_date_ref("explicit_20.05", today=FRI) == date(2027, 5, 20)


def test_explicit_feb29_invalid_year_rolls_forward():
    # 29.02 не существует в 2026/2027 — ближайший валидный високосный год
    assert resolve_date_ref("explicit_29.02", today=FRI) == date(2028, 2, 29)


def test_unknown_ref_raises():
    with pytest.raises(ValueError):
        resolve_date_ref("someday", today=FRI)


# ── time_ref ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, slot, expected", [
    (None, time(9, 0), True),          # нет предпочтения — подходит всё
    ("morning", time(9, 0), True),
    ("morning", time(11, 30), True),
    ("morning", time(12, 0), False),
    ("afternoon", time(12, 0), True),
    ("afternoon", time(16, 30), True),
    ("afternoon", time(17, 0), False),
    ("evening", time(17, 0), True),
    ("evening", time(11, 0), False),
    ("15:30", time(15, 30), True),
    ("15:30", time(15, 0), False),
])
def test_matches_time_ref(ref, slot, expected):
    assert matches_time_ref(ref, slot) is expected


def test_bad_time_ref_raises():
    with pytest.raises(ValueError):
        matches_time_ref("25:99", time(9, 0))
