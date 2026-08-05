# П-5б Редизайн выбора даты: список дней — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Живой тык 11.06: месячная эмодзи-сетка «очень тупо» — куча мёртвых пустых кнопок. Решение пользователя: паттерн его маникюр-бота (`~/manicure/keyboards/inline.py:dates_keyboard`) — список ТОЛЬКО доступных дней, 2 колонки, лейблы «11 июн · чт», занятых/выходных просто нет в списке. Сетка месяца удаляется целиком.

**Architecture:** `calendar_view.py` переписывается: `dates_view(days, start, today, has_more, lang)` → (текст «Выберите день 👇», ряды: 5×2 дней + пагинация). Лейблы — конвенция маникюр-бота (`день кор.месяц · кор.день-недели`, словари ru/uz скопированы). Пагинация: «Ещё даты ▶» → `cal:nav:<ISO следующего дня после последнего показанного>`; на не-первой странице «◀ Ближайшие» → `cal:nav:<today>`. `calendar_flow`: `_available_days_from(start, count=10, horizon=90)` — скан вперёд с ранним выходом, собирает первые 10 доступных дней; `cal:nav:` теперь несёт ISO-дату старта страницы (legacy `YYYY-MM` из старых сообщений → страница от 1 числа / today); `cal:noop`/`cal:none` остаются для живущих старых сообщений. Day-view не меняется, кроме кнопки «◀ К датам» (`cal:nav:<day>`). «Нет слотов 2 недели»: есть доступные дни в горизонте → список; нет вообще → текст без кнопок; FYI-дедуп как был.

**Контекст кодовой базы:** `calendar_view.py`, `calendar_flow.py`, `replies.py` (btn_back_calendar → «◀ К датам», + btn_more_dates/btn_first_dates; cal-caption в view), `tests/test_inline_calendar.py` (переписать сеточные тесты на списочные).

### Task 1 (единственная)

- [x] Тесты: dates_view — 2 колонки, лейблы «11 июн · чт» (ru) / «11 iyn · pa» (uz), пагинация (первая страница без «◀», последняя без «▶»); `cal:nav:<ISO>` → страница от даты; страница содержит только дни со слотами (вс нет); legacy `cal:nav:2026-07` не падает; day-view «◀ К датам»; «нет слотов» → список или чистый текст; прошедший cal:day → toast + первая страница.
- [x] Код: dates_view + словари, _available_days_from, parse nav (ISO + legacy), day-view кнопка, no_slots ветка.
- [x] Полный сьют зелёный; demo restore + `--check`; commit `feat(dialog): date picker redesign — available-days list (P5b)` + push; рестарт бота.
