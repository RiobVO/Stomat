# П-5 Инлайн-календарь — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** За «Другое время» → «📅 Выбрать дату»: эмодзи-сетка месяца (визуал «максимум эмодзи», одобрен 11.06), кликабельны только дни со слотами, навигация по месяцам (текущий + 2) редактированием на месте, day-view слотов сеткой 4-в-ряд в том же сообщении, ru/uz. «Нет слотов на 14 дней» → календарь пациенту + FYI владельцу (раз в день) вместо «передаю администратору». Шестой инкремент по спеке (секция 4).

**Architecture:** Новый mixin `_CalendarFlowMixin` (`dialog/calendar_flow.py`) — продолжение R4-структуры; чистое построение сетки — `dialog/calendar_view.py` (`month_view(year, month, available, today, lang)` → (caption, rows), тестируется без БД). Ячейки: 🟢N → `cal:day:<ISO>`, 📍 — сегодня, день без слотов → `cal:none` (toast «Свободного времени нет»), паддинг/прошедшее → `cal:noop` (молча). Навигация `cal:nav:<YYYY-MM>` с горизонтом текущий+2 (вне горизонта/мусор → перерисовать текущий / stale). Доступность месяца — `find_free_slots` по дням с ранним выходом (услуга из контекста, дефолт checkup; врач — если выбран). Day-view: ВСЕ слоты дня (день выбран явно), 4 в ряд (с врачами — 2 в ряд с именем), кнопки штатные `slot:`/`reslot:` (нумеруются edit_reply), внизу «◀ Календарь»; state booking_offer_slots/resched_offer_slots → выбор слота идёт существующим путём hold→confirm. Вход: 4-я кнопка в `_ask_date`. «Нет слотов 14 дней»: ответ-календарь + notify-FYI с in-memory дедупом раз в день (рестарт повторит — приемлемо для FYI).

**Контекст кодовой базы:** `fsm.py:_process_action` (ветка `cal`), `fsm.py:_ask_date`, `booking_flow.py:72–77` и `reschedule_flow.py:53–59` (ветки «нет слотов»), `shared_helpers.py:_collect_slots/_slot_label`, П-4: `Reply.button_rows/edit/toast`, сырые `cal:`-callback'и.

### Task 1: чистая сетка month_view

- [x] Тесты: июль-2026 (паддинг: 1.07 — среда), хедер дней недели noop, 🟢/📍/пустые, прошедшие дни noop, навигация (первый месяц — только ▶, последний — только ◀, средний — обе), uz-локализация месяцев/дней.
- [x] Код: `calendar_view.py` (MONTHS_AHEAD=2, словари месяцев/дней ru/uz, month_view), строки cal_caption/cal_no_slots/cal_past_day/btn_pick_date/btn_back_calendar/no_slots_calendar ru/uz.

### Task 2: mixin календаря + доступность + day-view

- [x] Тесты (DialogEngine): «📅 Выбрать дату» в ask_date; `cal:nav` → edit-Reply с 🟢 по графику (вс — пустое); nav за горизонт → текущий месяц; `cal:day` → ВСЕ слоты дня + «◀ Календарь», state booking_offer_slots, дальше штатный hold; прошедший день → toast + текущий месяц; `cal:noop` → пустой Reply, `cal:none` → toast; resched → reslot-кнопки и state resched; мусор → stale.
- [x] Код: `_CalendarFlowMixin` (_on_calendar, _calendar_reply, _calendar_day_reply, _available_days), ветка `cal` в `_process_action`, кнопка в `_ask_date`.

### Task 3: «нет слотов 14 дней» → календарь + FYI раз в день

- [x] Тесты: врач без графика → ответ с календарём и nav-кнопками (не «передаю администратору»), state booking_collect, notify один; повторный заход в тот же день → notify не растёт; resched-вариант остаётся в resched.
- [x] Код: ветки booking/resched → `_no_slots_calendar_reply`, in-memory дедуп FYI.

### Task 4: финал

- [x] Полный сьют зелёный (+ серия для конкурентных), commit `feat(dialog): inline emoji calendar for date picking (P5)` + push.
