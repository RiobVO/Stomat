# П-3 «Клиника работает с X до Y» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Запрошенное точное время вне рабочего окна («а в 21 могу?») → явная строка «Клиника работает с {open} до {close}.» ПЕРЕД слотами — пациент понимает, почему ему предлагают 10:00. Пятый инкремент по спеке (секция 3).

**Architecture:** `_offer_body()` получает `time_ref`; точное HH:MM (`exact_time_ref`, окна morning/evening не считаются) сравнивается с `open_bounds()` дня `max(asked, today)` в локальном времени. Строка НЕ добавляется, если уже сработала «Сейчас клиника закрыта» (две строки о закрытости — шум) и если день целиком закрыт (существующий скан к ближайшему дню). Оба вызова (`_offer_slots`, `_offer_resched_slots`) передают `ctx.time_ref`.

**Контекст кодовой базы:** `shared_helpers.py:132–152` (_offer_body, _closed_now), `dates.py:64–73` (exact_time_ref), вызовы — booking_flow.py:95, reschedule_flow.py:69.

### Task 1 (единственная)

- [x] Тесты (`tests/test_outside_hours.py`): 21:00 будущего рабочего дня → строка «с 09:00 до 18:00» + слоты предложены; 14:00 (в окне) → строки НЕТ; «evening» → строки НЕТ; «сегодня в 21» поздно вечером → только «Сейчас клиника закрыта» (без дубля); перенос («в 6 утра») → строка есть.
- [x] Код: `_day_window` хелпер (open_bounds → локальные datetime), `_offer_body(..., time_ref)`, строка `outside_hours` ru/uz, оба вызова с ctx.time_ref.
- [x] Полный сьют зелёный; commit `feat(dialog): explicit working-hours line for off-hours requests (P3)` + push.
