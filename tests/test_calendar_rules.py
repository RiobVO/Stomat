"""Чистые календарные правила: график, обед, выходные, праздники. Без БД."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from navbat.scheduling.calendar_rules import day_intervals, slot_candidates

from conftest import WORKING_INTERVALS, at_tashkent, next_monday, next_sunday

TASHKENT = ZoneInfo("Asia/Tashkent")


def test_two_shifts_with_lunch_break():
    day = next_monday()
    intervals = day_intervals(WORKING_INTERVALS, day, TASHKENT, holidays=set())
    assert intervals == [
        (at_tashkent(day, "09:00"), at_tashkent(day, "13:00")),
        (at_tashkent(day, "14:00"), at_tashkent(day, "18:00")),
    ]
    # интервалы в UTC (Ташкент = UTC+5: 09:00 → 04:00 UTC)
    assert intervals[0][0].astimezone(timezone.utc).hour == 4


def test_holiday_gives_no_intervals():
    day = next_monday()
    assert day_intervals(WORKING_INTERVALS, day, TASHKENT, holidays={day}) == []


def test_day_off_gives_no_intervals():
    # воскресенья нет в working_intervals
    assert day_intervals(WORKING_INTERVALS, next_sunday(), TASHKENT, holidays=set()) == []


def test_candidates_respect_lunch_and_shift_bounds():
    day = next_monday()
    intervals = day_intervals(WORKING_INTERVALS, day, TASHKENT, holidays=set())
    slots = slot_candidates(intervals, duration_min=30, step_min=30)
    starts = {s.astimezone(TASHKENT).strftime("%H:%M") for s, _ in slots}
    assert "12:30" in starts            # последний утренний
    assert "13:00" not in starts        # обед
    assert "13:30" not in starts        # обед
    assert "14:00" in starts            # начало второй смены
    assert "17:30" in starts            # последний вечерний (30 мин до 18:00)
    assert "18:00" not in starts
    assert len(slots) == 16             # 8 утром + 8 вечером


def test_long_service_does_not_cross_shift_end():
    day = next_monday()
    intervals = day_intervals(WORKING_INTERVALS, day, TASHKENT, holidays=set())
    slots = slot_candidates(intervals, duration_min=60, step_min=30)
    starts = {s.astimezone(TASHKENT).strftime("%H:%M") for s, _ in slots}
    assert "12:00" in starts            # 12:00–13:00 влезает
    assert "12:30" not in starts        # 12:30–13:30 пересёк бы обед
    assert "17:30" not in starts        # 17:30–18:30 вылез бы за смену
    assert "17:00" in starts
    # длительность слота соблюдена
    start, end = slots[0]
    assert end - start == timedelta(minutes=60)
