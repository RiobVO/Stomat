"""date_ref/time_ref -> конкретные дата/окно. Чистые функции, без I/O.

LLM извлекает относительную ссылку, абсолютную дату считает этот код —
«сегодня» передаётся снаружи уже в таймзоне клиники.
"""
from __future__ import annotations

from datetime import date, time, timedelta

from navbat.nlu.schema import DATE_REF_RE, TIME_REF_RE

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# окна предпочтений: [от, до); evening — до конца суток
_TIME_WINDOWS = {
    "morning": (time(0, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time.max),
}


def resolve_date_ref(ref: str, today: date) -> date:
    """Дата по ссылке словаря date_ref. Кривая ссылка -> ValueError."""
    if not DATE_REF_RE.match(ref):
        raise ValueError(f"date_ref вне словаря: {ref!r}")
    if ref == "today":
        return today
    if ref == "tomorrow":
        return today + timedelta(days=1)
    if ref == "after_tomorrow":
        return today + timedelta(days=2)
    if ref == "next_week":
        # понедельник следующей ISO-недели (с понедельника — через 7 дней)
        return today + timedelta(days=7 - today.weekday())
    if ref.startswith("weekday_"):
        target = _WEEKDAYS.index(ref.removeprefix("weekday_"))
        return today + timedelta(days=(target - today.weekday()) % 7)
    # explicit_DD.MM: текущий год; прошедшая или несуществующая (29.02
    # в невисокосном) дата -> ближайший следующий год, где она валидна и впереди
    day, month = map(int, ref.removeprefix("explicit_").split("."))
    for year in range(today.year, today.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    raise ValueError(f"невозможная дата: {ref!r}")


def matches_time_ref(ref: str | None, slot_start: time) -> bool:
    """Подходит ли локальное время начала слота под предпочтение пациента."""
    if ref is None:
        return True
    if not TIME_REF_RE.match(ref):
        raise ValueError(f"time_ref вне словаря: {ref!r}")
    window = _TIME_WINDOWS.get(ref)
    if window:
        return window[0] <= slot_start < window[1]
    hours, minutes = map(int, ref.split(":"))
    return slot_start == time(hours, minutes)
