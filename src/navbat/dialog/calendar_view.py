"""Выбор даты (П-5б): список ТОЛЬКО доступных дней, 2 колонки.

Редизайн по живому тыку 11.06: месячная сетка показывала мёртвые пустые
ячейки — «очень тупо». Паттерн взят из маникюр-бота пользователя
(dates_keyboard): каждая кнопка — реальный день со слотами, лейбл
«11 июн · чт», занятых/выходных просто нет в списке. Пагинация «Ещё
даты ▶» листает вперёд (горизонт у вызывающего), «◀ Ближайшие» —
возврат на первую страницу. Чистое view — доступность считает
calendar_flow; callback'и — сырые короткие cal:* (П-4).
"""
from __future__ import annotations

from datetime import date, timedelta

from navbat.dialog.replies import Button, t

DAYS_PER_PAGE = 10   # 5 рядов по 2
DAYS_PER_ROW = 2
HORIZON_DAYS = 90    # дальше трёх месяцев вперёд не листаем

# конвенция лейблов — как в маникюр-боте: «11 июн · чт» / «11 iyn · pa»
_MONTHS_SHORT = {
    "ru": ("янв", "фев", "мар", "апр", "май", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"),
    "uz": ("yan", "fev", "mar", "apr", "may", "iyn",
           "iyl", "avg", "sen", "okt", "noy", "dek"),
}
_WEEKDAYS_SHORT = {
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
    "uz": ("du", "se", "ch", "pa", "ju", "sh", "ya"),
}
_CAPTION = {"ru": "📅 <b>Выберите день</b> 👇", "uz": "📅 <b>Kunni tanlang</b> 👇"}


def day_label(day: date, lang: str) -> str:
    return (f"{day.day} {_MONTHS_SHORT[lang][day.month - 1]}"
            f" · {_WEEKDAYS_SHORT[lang][day.weekday()]}")


def dates_view(days: list[date], start: date, today: date, has_more: bool,
               lang: str) -> tuple[str, tuple[tuple[Button, ...], ...]]:
    """Страница доступных дней: (текст сообщения, ряды inline-кнопок).

    days — ТОЛЬКО дни со свободными слотами (никаких заглушек);
    start > today означает не-первую страницу (даём «◀ Ближайшие»)."""
    rows: list[tuple[Button, ...]] = []
    for i in range(0, len(days), DAYS_PER_ROW):
        rows.append(tuple(
            Button(day_label(day, lang), f"cal:day:{day.isoformat()}")
            for day in days[i:i + DAYS_PER_ROW]
        ))
    nav: list[Button] = []
    if start > today:
        nav.append(Button(t("btn_first_dates", lang),
                          f"cal:nav:{today.isoformat()}"))
    if has_more and days:
        following = max(days) + timedelta(days=1)
        nav.append(Button(t("btn_more_dates", lang),
                          f"cal:nav:{following.isoformat()}"))
    if nav:
        rows.append(tuple(nav))
    return _CAPTION[lang], tuple(rows)
