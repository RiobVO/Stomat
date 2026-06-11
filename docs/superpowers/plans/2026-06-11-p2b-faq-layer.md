# П-2б FAQ-слой + дайджест вопросов — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Бот сам отвечает на два самых частых бытовых вопроса (часы работы — из графиков, адрес — новое поле) без LLM и без админа; вопросы, на которые ответа нет, копятся и приходят владельцу в вечернем дайджесте. Третий инкремент по спеке (секция 2, «FAQ-слой» и «дайджест вопросов»).

**Architecture:** Детекторы часов/адреса — чистые функции в dialog_common (стиль П-1/П-2а). В `_route_intent` FAQ-проверка стоит ПОСЛЕ booking_like-бэкстопа (вопрос с date_ref продолжает получать слоты), но ДО детектора наличия — иначе «ish vaqti?» съедается маркером «vaqt». Ответ оборачивается `_with_reprompt` (вопрос вбок не сбивает сценарий). Часы: `open_bounds()` на сегодня, при закрытом дне — скан до 14 дней к ближайшему рабочему. Адрес: `clinic.address` (nullable); не задан → детектор пропускает, путь идёт дальше. Неотвеченные вопросы — таблица `unanswered_question` (RLS, без chat_id — анонимно; телефоны маскируются `redact_phones` из wrappers), блок в дайджесте (cap 10), retention 90 дней той же чисткой.

**Tech Stack:** alembic (0016), stdlib. Ноль новых зависимостей.

**Контекст кодовой базы:**
- `src/navbat/dialog/fsm.py:~270` — `_route_intent`: FAQ-ветка между booking_like и availability; `_answer_question(..., message)` — проброс текста (3 точки вызова: fsm:284, booking_flow:129,147).
- `migrations/versions/0006_reminders.py:49–58` — паттерн RLS/grants для новой таблицы.
- `src/navbat/nlu/wrappers.py:36,55` — `_PHONE_RE` → публичная `redact_phones`.
- `src/navbat/reminders.py:167–198` — `maybe_send_digest`: блок вопросов после render_stats.
- `src/navbat/retention.py` — третий DELETE.
- `src/navbat/onboard.py:352+` — `--address`; `src/navbat/supervisor.py:113+` — `run_check`: [OK]-строка с подсказкой при пустом адресе.
- `tests/test_stats.py:137` — паттерн digest-теста (ReminderService + FakeTelegramAPI).

---

### Task 1: миграция 0016 + детекторы + FAQ-ответы + копилка вопросов

**Files:** Create `migrations/versions/0016_faq_layer.py`, `src/navbat/dialog/questions_repo.py`, `tests/test_faq_layer.py`; Modify `src/navbat/dialog/dialog_common.py`, `src/navbat/dialog/fsm.py`, `src/navbat/dialog/booking_flow.py`, `src/navbat/dialog/clinic_repo.py`, `src/navbat/dialog/replies.py`, `src/navbat/nlu/wrappers.py`, `tests/test_replies_uz.py`

- [x] Тесты: детекторы часы/адрес (позитив ru/uz, негатив); «ish vaqti?» → ЧАСЫ, не кнопки дат (порядок FAQ > availability); часы в рабочий день → «с 09:00 до 18:00»; часы в выходной (вс) → «ближайший рабочий день {date}»; адрес задан → адрес; адрес NULL → not_understood; FAQ посреди слотов → ответ + повтор слотов; неотвеченный вопрос → строка в unanswered_question с маскировкой телефона.
- [x] Миграция: clinic.address text NULL; unanswered_question (id bigserial, clinic_id FK, question text, at timestamptz default now()) + индекс (clinic_id, at) + RLS FORCE + policy + grants + sequence grant.
- [x] Код: `mentions_hours_question`/`mentions_address_question`; `redact_phones` в wrappers (переиспользована в DeidentifyingExtractor); `clinic_repo.clinic_address`; `questions_repo.add/for_day`; `_faq_answer`+`_hours_reply` в fsm; `_answer_question(..., message)` пишет вопрос в копилку; строки hours_today/hours_next/clinic_address ru/uz.
- [x] Run → PASS.

### Task 2: дайджест + retention + онбординг

**Files:** Modify `src/navbat/reminders.py`, `src/navbat/stats.py`, `src/navbat/retention.py`, `src/navbat/onboard.py`, `src/navbat/supervisor.py`; Tests: `tests/test_faq_layer.py`, `tests/test_retention.py`

- [x] Тесты: дайджест содержит «Вопросы без ответа» при наличии и НЕ содержит при пустом дне; retention чистит старые вопросы; onboard --address пишет поле.
- [x] Код: `render_questions` в stats.py (cap 10 + «…и ещё N»); вызов в maybe_send_digest; DELETE в retention; --address в onboard; [OK]-подсказка в run_check.
- [x] Run → PASS.

### Task 3: финал

- [x] Полный сьют → зелёный; commit `feat(dialog): FAQ layer (hours/address) + unanswered questions digest (P2b)` + push.

## Definition of Done (П-2б)

- [x] «до скольки работаете?» и «manzil?» отвечаются ботом без LLM и без админа.
- [x] Незнаемый вопрос копится и приходит в вечернем дайджесте (маскировка телефонов, cap 10).
- [x] Адрес настраивается онбордингом; --check подсказывает, если пуст.
- [x] Полный сьют зелёный; коммит в origin/master.
