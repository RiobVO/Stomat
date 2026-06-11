"""Инлайн-календарь выбора даты (П-5): чистое построение сетки месяца.

Визуал «максимум эмодзи» (одобрен пользователем 11.06): 🟢N — день со
свободным слотом (кликабелен), 📍 — сегодня, прочие ячейки пусты.
Callback'и — сырые короткие cal:* (П-4): живут дольше tg_actions-map'а.
Доступность дней считает вызывающий (calendar_flow) — здесь только view,
тестируемый без БД.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date

from navbat.dialog.replies import Button

MONTHS_AHEAD = 2  # навигация: текущий месяц + 2 вперёд

# Брайлевский пробел: Telegram требует непустой text у кнопки,
# а ячейка должна выглядеть пустой
BLANK = "⠀"

WEEKDAYS = {
    "ru": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
    "uz": ("Du", "Se", "Cho", "Pa", "Ju", "Sha", "Ya"),
}
MONTH_NAMES = {
    "ru": ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
           "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"),
    "uz": ("Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul",
           "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"),
}
CAPTION = {
    "ru": "📅 {month} {year}\n🟢 — есть свободное время",
    "uz": "📅 {month} {year}\n🟢 — bo'sh vaqt bor",
}


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def months_between(start: date, year: int, month: int) -> int:
    return (year - start.year) * 12 + month - start.month


def month_view(year: int, month: int, available: set[date], today: date,
               lang: str) -> tuple[str, tuple[tuple[Button, ...], ...]]:
    """Сетка месяца: (текст сообщения, ряды inline-кнопок).

    Кликабельны только дни из available (🟢 / 📍 для сегодня); будущий
    день без слотов — cal:none (toast «времени нет»), паддинг и прошлое —
    cal:noop (молча)."""
    rows: list[tuple[Button, ...]] = [
        tuple(Button(d, "cal:noop") for d in WEEKDAYS[lang])
    ]
    for week in _calendar.Calendar().monthdatescalendar(year, month):
        cells = []
        for day in week:
            if day.month != month or day < today:
                cells.append(Button(BLANK, "cal:noop"))
            elif day in available:
                mark = "📍" if day == today else "🟢"
                cells.append(Button(f"{mark}{day.day}",
                                    f"cal:day:{day.isoformat()}"))
            elif day == today:
                cells.append(Button("📍", "cal:none"))
            else:
                cells.append(Button(BLANK, "cal:none"))
        rows.append(tuple(cells))

    nav: list[Button] = []
    offset = months_between(today, year, month)
    if offset > 0:
        py, pm = add_months(year, month, -1)
        nav.append(Button(f"◀ {MONTH_NAMES[lang][pm - 1]}",
                          f"cal:nav:{py:04d}-{pm:02d}"))
    if offset < MONTHS_AHEAD:
        ny, nm = add_months(year, month, 1)
        nav.append(Button(f"{MONTH_NAMES[lang][nm - 1]} ▶",
                          f"cal:nav:{ny:04d}-{nm:02d}"))
    if nav:
        rows.append(tuple(nav))

    caption = CAPTION[lang].format(month=MONTH_NAMES[lang][month - 1],
                                   year=year)
    return caption, tuple(rows)
