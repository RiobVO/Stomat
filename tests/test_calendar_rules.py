"""Чистые календарные правила: график, обед, выходные, праздники. Без БД."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from navbat.scheduling.calendar_rules import day_intervals, open_bounds, slot_candidates

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


# ── Рабочее окно дня клиники (для «сейчас закрыто») ──────────────────────────

def test_open_bounds_spans_lunch_break():
    # окно дня — от первого открытия до последнего закрытия: обед НЕ «закрыто»
    day = next_monday()
    assert open_bounds([WORKING_INTERVALS], day, TASHKENT, holidays=set()) == (
        at_tashkent(day, "09:00"), at_tashkent(day, "18:00"))


def test_open_bounds_union_of_doctors():
    day = next_monday()
    early = {"mon": [["08:00", "12:00"]]}
    late = {"mon": [["10:00", "19:00"]]}
    assert open_bounds([early, late], day, TASHKENT, holidays=set()) == (
        at_tashkent(day, "08:00"), at_tashkent(day, "19:00"))


def test_open_bounds_holiday_is_closed():
    day = next_monday()
    assert open_bounds([WORKING_INTERVALS], day, TASHKENT, holidays={day}) is None


def test_open_bounds_day_off_is_closed():
    assert open_bounds([WORKING_INTERVALS], next_sunday(), TASHKENT,
                       holidays=set()) is None


def test_open_bounds_no_doctors_is_closed():
    assert open_bounds([], next_monday(), TASHKENT, holidays=set()) is None


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
