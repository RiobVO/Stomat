# П-6 /stats глазами владельца — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/stats [7|30]` — периоды; блок «💰 Ценность» сверху (включая новую метрику «записи вне рабочих часов» — бот работал, пока клиника спала, и витринную «эскалаций: 0»), «⚙️ Служебное» кратко внизу. Вечерний дайджест — тот же рендер за день. Седьмой инкремент по спеке (секция 5).

**Architecture:** `collect_daily_stats` обобщается до `collect_stats(session, first, last, tz)` (день — частный случай-обёртка, дайджест не трогаем). «Вне рабочих часов»: confirm-аудиты периода, чей `at` вне `open_bounds()` своего локального дня (день целиком закрыт — тоже «вне часов»); считается кодом по строкам аудита (объёмы малые). Рендер: заголовок «📊 Сводка за {день} / за N дн. ({first}–{last})», ценность сверху, LLM/p95 одной строкой внизу; M2-семантика prevented_noshows/saved_revenue не меняется. Воркер: `/stats` парсит аргумент (1–90), мусор → подсказка формата.

**Контекст кодовой базы:** `stats.py:19–117` (DailyStats/collect/render; after_hours_booked — с дефолтом, чтобы не ломать конструкторы в тестах), `worker.py:135–137,401–411` (/stats), `reminders.py:184` (digest — через day-обёртку), `tests/test_stats.py` (seed_activity, FakeTelegramAPI).

### Task 1 (единственная)

- [x] Тесты: диапазон считает confirm'ы обоих дней, день — только свой; confirm в 22:00 пн → after_hours_booked=1, в 10:00 — нет; рендер содержит «💰», «вне рабочих часов» (и НЕ содержит при 0); `/stats 7` из админ-чата → заголовок периода; `/stats abc` → подсказка формата; пациентский «/stats 7» → NLU, не команда.
- [x] Код: collect_stats + обёртка, after-hours подсчёт (open_bounds), новый render_stats(stats, first, last=None), парсер /stats в воркере.
- [x] Полный сьют зелёный; commit `feat(stats): owner-view /stats with periods + after-hours bookings (P6)` + push.
