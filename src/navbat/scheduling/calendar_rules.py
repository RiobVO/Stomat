"""Чистые календарные правила: график → интервалы дня → кандидаты слотов.

Никакого I/O. Вход — данные клиники/врача, выход — aware-datetime в UTC.
Границы дня интерпретируются в таймзоне клиники, наружу отдаём UTC.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Interval = tuple[datetime, datetime]


def day_intervals(
    working_intervals: dict,
    day: date,
    tz: ZoneInfo,
    holidays: set[date],
) -> list[Interval]:
    """Рабочие интервалы дня в UTC. Праздник или отсутствующий день недели → []."""
    if day in holidays:
        return []
    spans = working_intervals.get(WEEKDAY_KEYS[day.weekday()], [])
    return [(_at(day, start, tz), _at(day, end, tz)) for start, end in spans]


def slot_candidates(
    intervals: list[Interval], duration_min: int, step_min: int = 30
) -> list[Interval]:
    """Кандидаты слотов на сетке: слот целиком внутри интервала (обед/конец смены
    непробиваемы по построению)."""
    duration = timedelta(minutes=duration_min)
    step = timedelta(minutes=step_min)
    slots: list[Interval] = []
    for start, end in intervals:
        cursor = start
        while cursor + duration <= end:
            slots.append((cursor, cursor + duration))
            cursor += step
    return slots


def _at(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hours, minutes), tz).astimezone(timezone.utc)
