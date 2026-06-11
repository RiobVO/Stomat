"""Выбор даты: месячная inline-сетка с маркерами (вариант B).

Пересмотр 11.06 (живой тык, полировка-3): список доступных дней (П-5б)
пользователь забраковал — «нужно листать, а ожидал сетку с точками».
Возврат варианта B Windows-дизайна: заголовок «Июнь 2026», шапка дней
недели, ряды пн–вс с заглушками по краям; свободный день — «•15»
(cal:day:ISO), занятый/прошлый — «15» (cal:noop → toast). Навигация
«◀»/«▶» по месяцам редактирует то же сообщение: в прошлое не листаемся,
вперёд — горизонт HORIZON_DAYS. Чистое view — свободные дни считает
calendar_flow; callback'и — сырые короткие cal:* (П-4).
"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Collection
from datetime import date, timedelta

from navbat.dialog.replies import Button, t

HORIZON_DAYS = 90    # дальше трёх месяцев вперёд не листаем

_MONTHS_FULL = {
    "ru": ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
           "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"),
    "uz": ("Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
           "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"),
}
_WEEKDAYS_SHORT = {
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
    "uz": ("du", "se", "ch", "pa", "ju", "sh", "ya"),
}
_LEGEND = {"ru": "• — есть свободное время", "uz": "• — bo'sh vaqt bor"}

_NOOP = "cal:noop"


def month_title(month: date, lang: str) -> str:
    return f"{_MONTHS_FULL[lang][month.month - 1]} {month.year}"


def month_view(month: date, free_days: Collection[date], today: date,
               horizon_end: date, lang: str
               ) -> tuple[str, tuple[tuple[Button, ...], ...]]:
    """Сетка месяца: (текст сообщения, ряды inline-кнопок).

    month — 1-е число месяца; free_days — дни СО свободными слотами
    (прошлые/занятые рисуются мёртвыми ячейками cal:noop)."""
    note = _LEGEND[lang] if free_days else t("cal_no_free_days_month", lang)
    caption = f"📅 <b>{month_title(month, lang)}</b>\n{note}"
    rows: list[tuple[Button, ...]] = [
        tuple(Button(wd, _NOOP) for wd in _WEEKDAYS_SHORT[lang])]
    cells = [Button(" ", _NOOP)] * month.weekday()  # заглушки до 1-го числа
    for n in range(1, monthrange(month.year, month.month)[1] + 1):
        day = month.replace(day=n)
        cells.append(Button(f"•{n}", f"cal:day:{day.isoformat()}")
                     if day in free_days else Button(str(n), _NOOP))
    cells += [Button(" ", _NOOP)] * (-len(cells) % 7)  # хвост до полного ряда
    rows += [tuple(cells[i:i + 7]) for i in range(0, len(cells), 7)]
    nav: list[Button] = []
    if month > today.replace(day=1):
        prev = (month - timedelta(days=1)).replace(day=1)
        nav.append(Button("◀", f"cal:nav:{prev.isoformat()}"))
    following = (month + timedelta(days=32)).replace(day=1)
    if following <= horizon_end:
        nav.append(Button("▶", f"cal:nav:{following.isoformat()}"))
    if nav:
        rows.append(tuple(nav))
    return caption, tuple(rows)
